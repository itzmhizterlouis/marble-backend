import asyncio

import httpx
import pytest

from app.config import get_settings
from app.gemini import VIDEO_GENERATION_SCHEMA, GeminiClient, GeminiError
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


def test_first_pass_returns_copy_and_grounded_observations_in_one_request(monkeypatch):
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key")
    client = GeminiClient()
    captured = {}

    async def fake_generate_json(parts, schema, **_kwargs):
        captured["parts"] = parts
        captured["schema"] = schema
        return {
            "shared_caption": "A handmade bag in the studio.",
            "hashtags": ["handmade"],
            "tiktok_caption": "",
            "instagram_caption": "A handmade bag in the studio.",
            "facebook_title": "",
            "facebook_caption": "",
            "youtube_title": "",
            "youtube_description": "",
            "video_observations": {
                "summary": "A creator shows a handmade bag.",
                "details": ["The bag is blue."],
                "uncertainties": ["The material is not clear."],
            },
        }, {"promptTokenCount": 120}

    monkeypatch.setattr(client, "_generate_json", fake_generate_json)
    candidate, observations, usage = asyncio.run(client.create_candidate(
        file={"uri": "files/test", "mimeType": "video/mp4"},
        current_caption="",
        hashtags=[],
        platforms=["instagram"],
    ))

    assert captured["schema"] is VIDEO_GENERATION_SCHEMA
    assert captured["parts"][0]["fileData"]["fileUri"] == "files/test"
    assert candidate["instagram_caption"] == "A handmade bag in the studio."
    assert "video_observations" not in candidate
    assert observations["details"] == ["The bag is blue."]
    assert usage["promptTokenCount"] == 120


def test_text_only_variant_uses_observations_without_video_part(monkeypatch):
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key")
    client = GeminiClient()
    captured = {}

    async def fake_generate(parts):
        captured["parts"] = parts
        return {"shared_caption": "A new opening"}, {}

    monkeypatch.setattr(client, "_generate", fake_generate)
    asyncio.run(client.adjust_candidate(
        {"shared_caption": "An old opening"},
        "regenerate",
        "Emphasize the blue stitching",
        video_observations={"summary": "A creator shows a bag.", "details": ["Blue stitching is visible."], "uncertainties": []},
        platforms=["instagram"],
    ))

    assert len(captured["parts"]) == 1
    assert "fileData" not in captured["parts"][0]
    assert "Blue stitching is visible" in captured["parts"][0]["text"]
    assert "Emphasize the blue stitching" in captured["parts"][0]["text"]


def test_invalid_observation_notes_do_not_discard_valid_copy(monkeypatch):
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key")
    client = GeminiClient()

    async def fake_generate_json(_parts, _schema, **_kwargs):
        return {
            "shared_caption": "A creator shows a bag.",
            "hashtags": [],
            "tiktok_caption": "",
            "instagram_caption": "A creator shows a bag.",
            "facebook_title": "",
            "facebook_caption": "",
            "youtube_title": "",
            "youtube_description": "",
            "video_observations": {"summary": "", "details": [], "uncertainties": []},
        }, {}

    monkeypatch.setattr(client, "_generate_json", fake_generate_json)
    candidate, observations, _usage = asyncio.run(client.create_candidate(
        file={"uri": "files/test", "mimeType": "video/mp4"},
        current_caption="",
        hashtags=[],
        platforms=["instagram"],
    ))

    assert candidate["instagram_caption"] == "A creator shows a bag."
    assert observations is None
