import hashlib
import hmac
import time

import pytest
import respx
from httpx import Response

from app.config import get_settings
from app.providers import ProviderError, UploadPostClient, verify_upload_post_signature
from app.tasks import provider_result_status


def test_provider_success_does_not_turn_a_queued_result_into_published():
    assert provider_result_status({"success": True, "message": "Queued"}) == "queued"
    assert provider_result_status({"success": True, "status": "processing"}) == "publishing"
    assert provider_result_status({"success": False, "status": "retryable"}) == "publishing"
    assert provider_result_status({"success": True, "status": "completed"}) == "published"


def test_webhook_signature(monkeypatch):
    body = b'{"event_id":"evt-1"}'
    monkeypatch.setattr(get_settings(), "upload_post_webhook_secret", "webhook-secret")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"webhook-secret", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    assert verify_upload_post_signature(body, f"sha256={signature}", timestamp)
    assert verify_upload_post_signature(body, "wrong", timestamp) is False
    assert verify_upload_post_signature(body, signature, "1") is False


@respx.mock
async def test_connection_manager_is_branded_and_limited_to_one_platform(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_post_api_key", "provider-test-key")
    monkeypatch.setattr(settings, "upload_post_base_url", "https://provider.test/api")
    monkeypatch.setattr(settings, "frontend_url", "https://marble.example")
    route = respx.post("https://provider.test/api/uploadposts/users/generate-jwt").mock(
        return_value=Response(200, json={"access_url": "https://provider.test/connect?token=jwt"})
    )

    access_url = await UploadPostClient().connection_access_url(
        "marble_creator", "instagram", "https://marble.example/accounts"
    )

    assert access_url == "https://provider.test/connect?token=jwt"
    payload = route.calls.last.request.content.decode()
    for expected in (
        '"platforms":["instagram"]',
        '"show_calendar":false',
        '"logo_image":"https://marble.example/assets/reverb-logo.png"',
        '"redirect_button_text":"Return to Reverb"',
        "Upload-Post",
    ):
        assert expected in payload
    assert "youtube" not in payload


@respx.mock
async def test_scheduled_submission_contains_all_platform_versions(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_post_api_key", "provider-test-key")
    monkeypatch.setattr(settings, "upload_post_base_url", "https://provider.test/api")
    route = respx.post("https://provider.test/api/upload").mock(
        return_value=Response(200, json={"job_id": "job-123"})
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")

    result = await UploadPostClient().publish_video(
        profile="marble_creator",
        video_path=video,
        post_id="post-123",
        revision=4,
        versions=[
            {"platform": "instagram", "caption": "Instagram copy", "title": None},
            {"platform": "youtube", "caption": "YouTube copy", "title": "Video title"},
        ],
        scheduled_at="2026-10-01T11:00:00+00:00",
        timezone="Africa/Lagos",
        facebook_page_id=None,
    )

    assert result["job_id"] == "job-123"
    request = route.calls.last.request
    assert request.headers["idempotency-key"] == "marble:post-123:4"
    assert request.headers["x-request-id"]
    body = request.content
    for expected in (
        b"instagram",
        b"youtube",
        b"Instagram copy",
        b"YouTube copy",
        b"Video title",
        b"2026-10-01T11:00:00+00:00",
        b"Africa/Lagos",
        request.headers["x-request-id"].encode(),
    ):
        assert expected in body
    assert b"tiktok" not in body


async def test_youtube_submission_requires_an_explicit_title(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")

    with pytest.raises(ProviderError, match="YouTube requires a title"):
        await UploadPostClient().publish_video(
            profile="marble_creator",
            video_path=video,
            post_id="post-123",
            revision=4,
            versions=[
                {"platform": "youtube", "caption": "The real caption", "title": None},
            ],
            scheduled_at=None,
            timezone=None,
            facebook_page_id=None,
        )


@respx.mock
async def test_non_youtube_submission_omits_upload_post_generic_title(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "upload_post_api_key", "provider-test-key")
    monkeypatch.setattr(settings, "upload_post_base_url", "https://provider.test/api")
    route = respx.post("https://provider.test/api/upload").mock(
        return_value=Response(200, json={"job_id": "job-456"})
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")

    await UploadPostClient().publish_video(
        profile="marble_creator",
        video_path=video,
        post_id="post-456",
        revision=1,
        versions=[
            {"platform": "instagram", "caption": "The real caption", "title": None},
        ],
        scheduled_at=None,
        timezone=None,
        facebook_page_id=None,
    )

    body = route.calls.last.request.content
    assert b'name="title"' not in body
    assert b"The real caption" in body
