import asyncio
import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import MediaAsset, Post, Publication, SocialConnection, SubtitleJob, User
from app.providers import UploadPostClient
from app.subtitle_worker import process
from app.subtitles import SubtitleError, make_ass, normalize_words, transcribe
from app.tasks import _cleanup_media, _publish_post, _retry_platform

WORDS = [{"text": "Hello", "start": 0.1, "end": 0.5}, {"text": "Reverb", "start": 0.5, "end": 1.2}]


def setup_draft(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "groq_api_key", "test-groq-key")
    monkeypatch.setattr("app.tasks.process_subtitles.delay", lambda *args: None)
    monkeypatch.setattr("app.posts.publish_post.delay", lambda *args: None)
    email = f"subtitles-{uuid.uuid4().hex[:10]}@example.com"
    auth = client.post(
        "/v1/auth/register", json={"email": email, "password": "creator123", "name": "Subtitle Tester"}
    ).json()
    headers = {"Authorization": f"Bearer {auth['access_token']}"}

    async def prepare():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == email))
            user.email_verified = True
            user.upload_post_profile = "subtitle-" + uuid.uuid4().hex
            db.add(SocialConnection(user_id=user.id, platform="tiktok", status="connected"))
            db.add(SocialConnection(user_id=user.id, platform="instagram", status="connected"))
            media = MediaAsset(
                user_id=user.id,
                original_name="original.mov",
                mime_type="video/quicktime",
                size_bytes=100,
                uploaded_bytes=100,
                storage_path="/tmp/original.mov",
                status="ready",
                duration_seconds=3,
                width=1080,
                height=1920,
                delete_after=datetime.now(UTC) + timedelta(days=7),
            )
            db.add(media)
            await db.commit()
            return media.id

    media_id = asyncio.run(prepare())
    payload = {
        "media_id": media_id,
        "caption": "A test caption",
        "versions": [{"platform": "tiktok", "caption": "A test caption"}],
    }
    post = client.post("/v1/posts", json=payload, headers=headers)
    assert post.status_code == 201, post.text
    return headers, media_id, post.json(), payload


def test_normalization_and_safe_render_text():
    with pytest.raises(SubtitleError, match="No speech"):
        normalize_words({"words": []}, 10)
    with pytest.raises(SubtitleError):
        normalize_words({"words": [{"word": "x", "start": float("nan"), "end": 2}]}, 10)
    normalized = normalize_words(
        {"words": [{"word": "Hi", "start": -0.1, "end": 0.5}, {"word": "there", "start": 0.4, "end": 2}]}, 1
    )
    assert normalized == [{"text": "Hi", "start": 0, "end": 0.5}, {"text": "there", "start": 0.5, "end": 1}]
    script = make_ass([{"text": r"{\pos(0,0)}", "start": 0, "end": 1}], 1080, 1920)
    assert r"\pos(0,0)" not in script
    assert r"\fscx112\fscy112" in script
    assert "PlayResY: 1920" in script


@pytest.mark.asyncio
async def test_groq_word_timestamp_contract_and_sanitized_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "groq_api_key", "secret-test-token")
    audio = tmp_path / "audio.flac"
    audio.write_bytes(b"flac")
    with respx.mock as mock:
        route = mock.post("https://api.groq.com/openai/v1/audio/transcriptions").mock(
            return_value=httpx.Response(200, json={"words": [{"word": "Hello", "start": 0.1, "end": 0.5}]})
        )
        words = await transcribe(audio, 2)
        assert words[0]["text"] == "Hello"
        request = route.calls[0].request
        assert request.headers["Authorization"] == "Bearer secret-test-token"
        assert b"whisper-large-v3-turbo" in request.content
        assert b"timestamp_granularities[]" in request.content and b"word" in request.content
        route.mock(return_value=httpx.Response(401, json={"error": "private provider content"}))
        with pytest.raises(SubtitleError) as error:
            await transcribe(audio, 2)
        assert "private" not in str(error.value) and "token" not in str(error.value)


def test_deduplication_ownership_publish_guard_and_edits(client, monkeypatch):
    headers, media_id, post, payload = setup_draft(client, monkeypatch)
    first = client.post(f"/v1/media/{media_id}/subtitles", json={}, headers=headers)
    assert first.status_code == 202, first.text
    job = first.json()
    assert client.post(f"/v1/media/{media_id}/subtitles", json={}, headers=headers).json()["id"] == job["id"]
    assert (
        client.patch(
            f"/v1/posts/{post['id']}/subtitles", json={"subtitle_job_id": job["id"]}, headers=headers
        ).status_code
        == 200
    )
    for mode in ("now", "scheduled"):
        response = client.post(
            f"/v1/posts/{post['id']}/publish",
            json={
                "mode": mode,
                "scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "timezone": "Africa/Lagos",
            },
            headers=headers,
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "subtitles_not_ready"
    other_headers, other_media, _, _ = setup_draft(client, monkeypatch)
    assert client.get(f"/v1/subtitles/{job['id']}", headers=other_headers).status_code == 404
    assert (
        client.patch(
            f"/v1/posts/{post['id']}/subtitles", json={"subtitle_job_id": job["id"]}, headers=other_headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/media/{other_media}/subtitles",
            json={"base_job_id": job["id"], "texts": ["x"]},
            headers=other_headers,
        ).status_code
        == 404
    )

    async def ready():
        async with SessionLocal() as db:
            stored = await db.get(SubtitleJob, job["id"])
            stored.words, stored.status, stored.storage_path = WORDS, "ready", "/tmp/subtitled.mp4"
            stored.width, stored.height, stored.size_bytes = 1080, 1920, 1000
            await db.commit()

    asyncio.run(ready())
    bad = client.post(
        f"/v1/media/{media_id}/subtitles", json={"base_job_id": job["id"], "texts": ["x"]}, headers=headers
    )
    assert bad.status_code == 422
    edit = client.post(
        f"/v1/media/{media_id}/subtitles",
        json={"base_job_id": job["id"], "texts": ["Hello", "Reverb!"]},
        headers=headers,
    ).json()
    assert edit["id"] != job["id"] and edit["words"][1]["text"] == "Reverb!"
    assert edit["words"][1]["start"] == 0.5
    assert client.get(f"/v1/subtitles/{job['id']}", headers=headers).json()["words"][1]["text"] == "Reverb"
    # Reloading a saved draft keeps its exact subtitle version.
    assert client.get(f"/v1/posts/{post['id']}", headers=headers).json()["subtitle_job_id"] == job["id"]
    assert (
        client.patch(
            f"/v1/posts/{post['id']}/subtitles", json={"subtitle_job_id": None}, headers=headers
        ).json()["subtitle_job_id"]
        is None
    )
    assert (
        client.post(f"/v1/posts/{post['id']}/publish", json={"mode": "now"}, headers=headers).status_code
        == 202
    )


@pytest.mark.parametrize("mode", ["now", "scheduled", "retry"])
def test_publishing_uses_selected_video(client, monkeypatch, tmp_path, mode):
    headers, media_id, post, _ = setup_draft(client, monkeypatch)
    rendered = tmp_path / "captioned.mp4"
    rendered.write_bytes(b"rendered content")
    response = client.post(f"/v1/media/{media_id}/subtitles", json={}, headers=headers).json()

    async def ready():
        async with SessionLocal() as db:
            job = await db.get(SubtitleJob, response["id"])
            job.status, job.words, job.storage_path = "ready", WORDS, str(rendered)
            job.width, job.height, job.size_bytes = 1080, 1920, rendered.stat().st_size
            if mode == "retry":
                draft = await db.get(Post, post["id"])
                draft.subtitle_job_id = job.id
                draft.status = "publishing"
                db.add(Publication(post_id=draft.id, platform="tiktok", status="queued"))
            await db.commit()

    asyncio.run(ready())
    captured = []

    async def publish_video(self, **kwargs):
        captured.append(kwargs)
        assert kwargs["video_path"].read_bytes() == b"rendered content"
        return {
            "request_id": "subtitle-request",
            "job_id": "subtitle-schedule" if mode == "scheduled" else None,
        }

    monkeypatch.setattr(UploadPostClient, "publish_video", publish_video)
    if mode == "retry":
        asyncio.run(_retry_platform(post["id"], "tiktok"))
    else:
        client.patch(
            f"/v1/posts/{post['id']}/subtitles", json={"subtitle_job_id": response["id"]}, headers=headers
        )
        result = client.post(
            f"/v1/posts/{post['id']}/publish",
            json={
                "mode": mode,
                "scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "timezone": "Africa/Lagos",
            },
            headers=headers,
        )
        assert result.status_code == 202, result.text
        asyncio.run(_publish_post(post["id"]))
    assert len(captured) == 1
    assert bool(captured[0]["scheduled_at"]) == (mode == "scheduled")


def test_worker_caches_words_recovers_render_failure_and_cleans_up(client, monkeypatch, tmp_path):
    headers, media_id, post, _ = setup_draft(client, monkeypatch)
    original = tmp_path / "source.mp4"
    original.write_bytes(b"original")

    async def source():
        async with SessionLocal() as db:
            asset = await db.get(MediaAsset, media_id)
            asset.storage_path = str(original)
            await db.commit()

    asyncio.run(source())
    job = client.post(f"/v1/media/{media_id}/subtitles", json={}, headers=headers).json()
    calls = []

    async def transcript(*args):
        calls.append("groq")
        return WORDS

    monkeypatch.setattr("app.subtitle_worker.extract_audio", lambda *args: None)
    monkeypatch.setattr("app.subtitle_worker.transcribe", transcript)

    def fail(*args):
        raise RuntimeError("private render details")

    monkeypatch.setattr("app.subtitle_worker.render_video", fail)

    async def run():
        async with SessionLocal() as db:
            stored = await db.get(SubtitleJob, job["id"])
            token = stored.run_token
        await process(job["id"], token)

    asyncio.run(run())
    failed = client.get(f"/v1/subtitles/{job['id']}", headers=headers).json()
    assert failed["status"] == "failed" and failed["words"] == WORDS
    assert "private" not in failed["error_message"]
    retry = client.post(f"/v1/media/{media_id}/subtitles", json={"retry": True}, headers=headers).json()
    assert retry["id"] == job["id"]
    monkeypatch.setattr(
        "app.subtitle_worker.render_video", lambda source, output, words: output.write_bytes(b"captioned")
    )
    monkeypatch.setattr("app.subtitle_worker.probe_video", lambda path: (3, 1080, 1920))
    asyncio.run(run())
    asyncio.run(run())
    assert calls == ["groq"]
    ready = client.get(f"/v1/subtitles/{job['id']}", headers=headers).json()
    assert ready["status"] == "ready" and ready["preview_url"]
    preview = client.get(ready["preview_url"])
    assert preview.status_code == 200 and preview.content == b"captioned"
    assert client.get(ready["preview_url"].split("?")[0] + "?token=invalid").status_code == 401
    assert original.read_bytes() == b"original"

    async def expire():
        async with SessionLocal() as db:
            asset = await db.get(MediaAsset, media_id)
            asset.delete_after = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        await _cleanup_media()

    asyncio.run(expire())
    assert client.get(f"/v1/subtitles/{job['id']}", headers=headers).json()["status"] == "expired"
    assert client.get(ready["preview_url"]).status_code == 404


@pytest.mark.skipif(
    not shutil.which("ffmpeg")
    or " ass " not in subprocess.run(["ffmpeg", "-filters"], capture_output=True, text=True).stdout,
    reason="FFmpeg with libass required (covered in Docker)",
)
def test_actual_render_preserves_audio_and_phone_rotation(tmp_path):
    from app.media import probe_video
    from app.subtitles import render_video

    source = tmp_path / "source.mp4"
    rotated = tmp_path / "phone.mov"
    output = tmp_path / "output.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=640x360:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-display_rotation",
            "90",
            "-i",
            str(source),
            "-c",
            "copy",
            str(rotated),
        ],
        check=True,
    )
    render_video(rotated, output, WORDS)
    assert probe_video(output) == (2, 360, 640)
    streams = json.loads(
        subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)])
    )["streams"]
    assert any(stream["codec_type"] == "audio" for stream in streams)
    assert "Hello" in (tmp_path / "captions.ass").read_text()


def test_queue_failure_and_interrupted_job_are_retryable(client, monkeypatch):
    from celery.exceptions import CeleryError

    from app.subtitle_worker import reconcile

    headers, mid, _, _ = setup_draft(client, monkeypatch)

    def unavailable(*args):
        raise CeleryError("queue unavailable")

    monkeypatch.setattr("app.tasks.process_subtitles.delay", unavailable)
    assert client.post(f"/v1/media/{mid}/subtitles", json={}, headers=headers).status_code == 503

    async def check():
        async with SessionLocal() as db:
            job = await db.scalar(select(SubtitleJob).where(SubtitleJob.media_id == mid))
            assert job.status == "failed"
            return job.id

    jid = asyncio.run(check())
    monkeypatch.setattr("app.tasks.process_subtitles.delay", lambda *args: None)
    response = client.post(f"/v1/media/{mid}/subtitles", json={"retry": True}, headers=headers)
    assert response.json()["id"] == jid and response.json()["status"] == "queued"

    async def interrupt():
        async with SessionLocal() as db:
            job = await db.get(SubtitleJob, jid)
            job.status = "rendering"
            job.updated_at = datetime.now(UTC) - timedelta(minutes=41)
            await db.commit()
        await reconcile()

    asyncio.run(interrupt())
    failed = client.get(f"/v1/subtitles/{jid}", headers=headers).json()
    assert failed["status"] == "failed" and "interrupted" in failed["error_message"]


def test_private_r2_output_preview_and_expiry(client, monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    from app.storage import R2Storage

    headers, mid, _, _ = setup_draft(client, monkeypatch)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")

    async def use_r2():
        async with SessionLocal() as db:
            asset = await db.get(MediaAsset, mid)
            asset.storage_backend, asset.storage_path, asset.object_key = "r2", "", "source-key"
            await db.commit()

    asyncio.run(use_r2())
    job = client.post(f"/v1/media/{mid}/subtitles", json={}, headers=headers).json()

    @asynccontextmanager
    async def materialize(**kwargs):
        assert kwargs["object_key"] == "source-key"
        yield source

    monkeypatch.setattr("app.subtitle_worker.materialize_object", materialize)
    monkeypatch.setattr("app.subtitle_worker.extract_audio", lambda *args: None)

    async def transcript(*args):
        return WORDS

    monkeypatch.setattr("app.subtitle_worker.transcribe", transcript)
    monkeypatch.setattr(
        "app.subtitle_worker.render_video", lambda src, out, words: out.write_bytes(b"rendered")
    )
    monkeypatch.setattr("app.subtitle_worker.probe_video", lambda path: (3, 1080, 1920))
    monkeypatch.setattr(R2Storage, "__init__", lambda self, *args: None)
    saved, deleted = {}, []

    async def put(self, path, key, content_type):
        saved[key] = path.read_bytes()
        assert content_type == "video/mp4"

    async def sign(self, key):
        assert key in saved
        return "https://example.com/signed-test-video"

    async def delete(self, key):
        deleted.append(key)

    monkeypatch.setattr(R2Storage, "put_file", put)
    monkeypatch.setattr(R2Storage, "presign_download", sign)
    monkeypatch.setattr(R2Storage, "delete_object", delete)

    async def run():
        async with SessionLocal() as db:
            stored = await db.get(SubtitleJob, job["id"])
            token = stored.run_token
        await process(job["id"], token)

    asyncio.run(run())
    ready = client.get(f"/v1/subtitles/{job['id']}", headers=headers).json()
    assert ready["status"] == "ready"
    preview = client.get(ready["preview_url"], follow_redirects=False)
    assert (
        preview.status_code == 307 and preview.headers["location"] == "https://example.com/signed-test-video"
    )

    async def expire():
        async with SessionLocal() as db:
            asset = await db.get(MediaAsset, mid)
            asset.delete_after = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        await _cleanup_media()

    asyncio.run(expire())
    assert set(saved).issubset(set(deleted))
    assert client.get(ready["preview_url"], follow_redirects=False).status_code == 404


@pytest.fixture(autouse=True)
def empty_upload_history(monkeypatch):
    async def history(_self, **_kwargs):
        return {"history": [], "in_progress": []}
    monkeypatch.setattr(UploadPostClient, "upload_history", history)
