import asyncio

import httpx
import pytest

from app.config import get_settings
from app.gemini import GeminiClient, GeminiError
from app.storage import StorageError
from app.tasks import ai_failure_details


def test_ai_failure_details_are_safe_and_specific():
    timeout = GeminiError(
        "Gemini took too long to respond. Please try again.",
        code="gemini_generation_timeout",
    )
    assert ai_failure_details(timeout) == (
        "gemini_generation_timeout",
        "Gemini took too long to respond. Please try again.",
        None,
    )
    assert ai_failure_details(StorageError()) == (
        "ai_media_download_failed",
        "Could not retrieve your video. Please try again.",
        None,
    )
    assert ai_failure_details(RuntimeError("sensitive provider URL")) == (
        "ai_generation_failed",
        "We couldn't generate a suggestion. Please try again.",
        None,
    )


def test_gemini_generation_timeout_has_a_visible_error_and_stage(monkeypatch):
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key")
    stages = []

    def handler(request):
        raise httpx.ReadTimeout("", request=request)

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.gemini.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    client = GeminiClient(on_stage=stages.append)

    with pytest.raises(GeminiError) as failure:
        asyncio.run(
            client._generate_json(
                [{"text": "test"}],
                {"type": "object", "properties": {}},
                temperature=0.5,
                invalid_message="invalid draft",
            )
        )

    assert failure.value.code == "gemini_generation_timeout"
    assert str(failure.value)
    assert stages == ["gemini_generation"]


def test_gemini_provider_status_is_preserved_without_response_body(monkeypatch):
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key")

    def handler(_request):
        return httpx.Response(429, json={"error": {"message": "private upstream details"}})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.gemini.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )

    with pytest.raises(GeminiError) as failure:
        asyncio.run(
            GeminiClient()._generate_json(
                [{"text": "test"}],
                {"type": "object", "properties": {}},
                temperature=0.5,
                invalid_message="invalid draft",
            )
        )

    assert failure.value.code == "gemini_generation_rate_limited"
    assert failure.value.status_code == 429
    assert "private upstream details" not in str(failure.value)


def test_gemini_upload_reports_each_stage(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key")
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"video")
    stages = []

    def handler(request):
        if request.url.path == "/upload/v1beta/files":
            return httpx.Response(200, headers={"X-Goog-Upload-URL": "https://example.invalid/upload"})
        assert request.url.path == "/upload"
        return httpx.Response(
            200, json={"file": {"name": "files/test", "uri": "files/test", "state": "ACTIVE"}}
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.gemini.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    result = asyncio.run(GeminiClient(on_stage=stages.append).upload_file(video, "video/mp4"))

    assert result["state"] == "ACTIVE"
    assert stages == ["gemini_upload_session", "gemini_upload", "gemini_processing"]
