import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Text, select, update
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import SessionLocal, task_engine
from app.models import MediaAsset, Post, PostNotification, Publication, SocialConnection, User
from app.providers import UploadPostClient
from app.storage import StoredPart
from app.tasks import _process_media, _reconcile_active, _retry_platform


def register(client, email: str) -> dict:
    response = client.post(
        "/v1/auth/register",
        json={"name": "Reverb Creator", "email": email, "password": "creator123"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def verify_and_connect(
    email: str, media_id: str | None = None, platforms: tuple[str, ...] = ("tiktok",)
) -> None:
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        user.email_verified = True
        for platform in platforms:
            db.add(SocialConnection(user_id=user.id, platform=platform, status="connected"))
        if media_id:
            media = await db.get(MediaAsset, media_id)
            media.status = "ready"
            media.duration_seconds = 12
            media.width = 1080
            media.height = 1920
        await db.commit()


async def mark_media_ready(media_id: str) -> None:
    async with SessionLocal() as db:
        media = await db.get(MediaAsset, media_id)
        media.status = "ready"
        media.duration_seconds = 12
        media.width = 1080
        media.height = 1920
        await db.commit()


def test_verification_token_is_single_use(client, monkeypatch):
    delivered = []

    async def capture_email(_email, _name, token):
        delivered.append(token)

    monkeypatch.setattr("app.auth.send_verification_email", capture_email)
    auth = register(client, "verify@example.com")
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    assert len(delivered) == 1
    verified = client.post("/v1/auth/verify", json={"token": delivered[0]}, headers=headers)
    assert verified.status_code == 200
    assert verified.json()["email_verified"] is True
    reused = client.post("/v1/auth/verify", json={"token": delivered[0]}, headers=headers)
    assert reused.status_code == 400
    assert reused.json()["code"] == "invalid_or_expired_token"


def test_connection_metadata_and_management_handoff(client, monkeypatch):
    email = "connection-trust@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("instagram",)))

    async def connection_manager(_self, username, platform, redirect_url):
        assert username.startswith("marble_")
        assert platform == "instagram"
        assert redirect_url.endswith("/accounts?connect_platform=instagram")
        return "https://app.upload-post.com/connect?token=trust-token"

    monkeypatch.setattr(UploadPostClient, "connection_access_url", connection_manager)
    response = client.get("/v1/connections", headers=headers)
    assert response.status_code == 200
    instagram = next(item for item in response.json() if item["platform"] == "instagram")
    assert instagram["provider"] == "upload_post"
    assert "connected_at" in instagram
    assert "last_used_at" in instagram

    manage = client.post("/v1/connections/instagram/manage", headers=headers)
    assert manage.status_code == 200
    assert manage.json()["authorize_url"].startswith("https://app.upload-post.com/connect")


def test_connection_authorize_keeps_upload_post_hosted_access_url(client, monkeypatch):
    email = "connection-mobile@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=()))

    async def connection_manager(_self, username, platform, redirect_url):
        assert username.startswith("marble_")
        assert platform == "tiktok"
        assert redirect_url.endswith("/accounts?connect_platform=tiktok")
        return "https://app.upload-post.com/connect?token=mobile-safe-token"

    async def profile(_self, _username):
        return {"profile": {"social_accounts": {}}}

    monkeypatch.setattr(UploadPostClient, "get_profile", profile)
    monkeypatch.setattr(UploadPostClient, "connection_access_url", connection_manager)
    response = client.post("/v1/connections/tiktok/authorize", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"authorize_url": "https://app.upload-post.com/connect?token=mobile-safe-token"}


def test_connection_sync_exposes_public_handles_and_preserves_provider_ids(client, monkeypatch):
    email = "connection-handle@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("tiktok", "youtube")))

    async def profile(_self, username):
        assert username.startswith("marble_")
        return {
            "profile": {
                "social_accounts": {
                    "tiktok": {
                        "username": "-000g91dgwXgwtNwc-V8dolLsQJMUY3MQAfv",
                        "handle": "itsuokormarvellou",
                        "display_name": "Itsuokor Marvellous",
                    },
                    "youtube": {
                        "username": "UC2wycYDhEg2a__i-o123456",
                        "handle": "marvellousoshorenoya1175",
                        "display_name": "Marvellous Oshorenoya",
                    },
                }
            }
        }

    monkeypatch.setattr(UploadPostClient, "get_profile", profile)
    response = client.post("/v1/connections/sync", headers=headers)
    assert response.status_code == 200, response.text
    accounts = {item["platform"]: item for item in response.json()}
    assert accounts["tiktok"]["username"].startswith("-000g")
    assert accounts["tiktok"]["handle"] == "itsuokormarvellou"
    assert accounts["tiktok"]["display_name"] == "Itsuokor Marvellous"
    assert accounts["youtube"]["username"].startswith("UC2wy")
    assert accounts["youtube"]["handle"] == "marvellousoshorenoya1175"
    assert accounts["youtube"]["display_name"] == "Marvellous Oshorenoya"

    async def assert_persisted():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == email))
            rows = {
                item.platform: item
                for item in await db.scalars(select(SocialConnection).where(SocialConnection.user_id == user.id))
            }
            assert rows["youtube"].provider_account_id == "UC2wycYDhEg2a__i-o123456"
            assert rows["youtube"].handle == "marvellousoshorenoya1175"

    asyncio.run(assert_persisted())


def test_connection_webhook_does_not_replace_public_handle_with_provider_id(client, monkeypatch):
    email = "connection-webhook-handle@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("youtube",)))

    async def seed_connection():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == email))
            connection = await db.scalar(
                select(SocialConnection).where(
                    SocialConnection.user_id == user.id,
                    SocialConnection.platform == "youtube",
                )
            )
            connection.provider_account_id = "UC2wycYDhEg2a__i-o123456"
            connection.username = "UC2wycYDhEg2a__i-o123456"
            connection.handle = "marvellousoshorenoya1175"
            connection.display_name = "Marvellous Oshorenoya"
            await db.commit()
            return user.upload_post_profile

    profile_username = asyncio.run(seed_connection())
    secret = "webhook-handle-secret"
    monkeypatch.setattr(get_settings(), "upload_post_webhook_secret", secret)
    connection_events = []

    async def capture_connection_event(user_id, platform):
        connection_events.append((user_id, platform))

    monkeypatch.setattr("app.webhooks.publish_connection_event", capture_connection_event)
    payload = json.dumps(
        {
            "event": "social_account_connected",
            "event_id": "connection-handle-webhook-1",
            "profile_username": profile_username,
            "platform": "youtube",
            "account_name": "UC2wycYDhEg2a__i-o123456",
        }
    ).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256
    ).hexdigest()
    delivered = client.post(
        "/v1/webhooks/upload-post",
        content=payload,
        headers={
            "X-Upload-Post-Signature": f"sha256={signature}",
            "X-Upload-Post-Timestamp": timestamp,
            "X-Upload-Post-Delivery": "connection-handle-delivery-1",
            "Content-Type": "application/json",
        },
    )
    assert delivered.status_code == 200, delivered.text
    assert connection_events and connection_events[0][1] == "youtube"

    connections = client.get("/v1/connections", headers=headers)
    youtube = next(item for item in connections.json() if item["platform"] == "youtube")
    assert youtube["handle"] == "marvellousoshorenoya1175"
    assert youtube["display_name"] == "Marvellous Oshorenoya"


def test_resumable_chunk_checksum_and_offset(client):
    auth = register(client, "upload@example.com")
    asyncio.run(verify_and_connect("upload@example.com"))
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    initialized = client.post(
        "/v1/media",
        json={"filename": "clip.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    )
    assert initialized.status_code == 201, initialized.text
    assert initialized.json()["chunk_size"] == 2 * 1024 * 1024
    media_id = initialized.json()["id"]
    chunk = b"abcdefgh"
    upload_headers = {
        **headers,
        "Content-Range": "bytes 0-7/8",
        "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
        "Content-Type": "application/octet-stream",
    }
    uploaded = client.put(f"/v1/media/{media_id}", content=chunk, headers=upload_headers)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["uploaded_bytes"] == 8
    duplicate = client.put(f"/v1/media/{media_id}", content=chunk, headers=upload_headers)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "invalid_upload_offset"


def test_r2_multipart_parts_are_signed_and_confirmed(client, monkeypatch):
    email = "r2-upload@example.com"
    auth = register(client, email)
    asyncio.run(verify_and_connect(email))
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "r2")

    class FakeR2Storage:
        def __init__(self, _settings=None):
            pass

        @staticmethod
        def object_key(user_id, media_id, suffix):
            return f"users/{user_id}/media/{media_id}/source{suffix}"

        async def create_multipart_upload(self, _key, _content_type):
            return "r2-" + "x" * 400

        async def presign_part(self, _key, _upload_id, _part_number):
            return "https://r2.example/signed-part"

        async def list_parts(self, _key, _upload_id):
            return [StoredPart(part_number=1, etag="fake-etag", size_bytes=8)]

    monkeypatch.setattr("app.media_routes.R2Storage", FakeR2Storage)
    initialized = client.post(
        "/v1/media",
        json={"filename": "clip.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    )
    assert initialized.status_code == 201, initialized.text
    assert initialized.json()["upload_mode"] == "r2"
    assert initialized.json()["uploaded_bytes"] == 0
    assert isinstance(MediaAsset.__table__.c.multipart_upload_id.type, Text)
    media_id = initialized.json()["id"]

    signed = client.post(f"/v1/media/{media_id}/parts/1/sign", headers=headers)
    assert signed.status_code == 200, signed.text
    assert signed.json()["part_number"] == 1
    assert signed.json()["url"] == "https://r2.example/signed-part"

    confirmed = client.post(
        f"/v1/media/{media_id}/parts/1/confirm",
        json={"part_number": 1, "etag": '"fake-etag"', "size_bytes": 8},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["uploaded_bytes"] == 8
    assert confirmed.json()["chunk_size"] == settings.r2_multipart_part_size_bytes


def test_r2_media_completion_processes_source_and_thumbnail(client, monkeypatch):
    email = "r2-complete@example.com"
    auth = register(client, email)
    asyncio.run(verify_and_connect(email))
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "r2")

    class FakeR2Storage:
        completed = False
        thumbnail_bytes = b""
        processing_path = None

        def __init__(self, _settings=None):
            pass

        @staticmethod
        def object_key(user_id, media_id, suffix):
            return f"users/{user_id}/media/{media_id}/source{suffix}"

        @staticmethod
        def thumbnail_key(user_id, media_id):
            return f"users/{user_id}/media/{media_id}/thumbnail.jpg"

        async def create_multipart_upload(self, _key, _content_type):
            return "fake-upload-id"

        async def list_parts(self, _key, _upload_id):
            return [StoredPart(part_number=1, etag="fake-etag", size_bytes=8)]

        async def complete_multipart_upload(self, _key, _upload_id, _parts):
            FakeR2Storage.completed = True

        async def download_to_path(self, _key, destination):
            FakeR2Storage.processing_path = destination
            destination.write_bytes(b"fake-video")

        async def put_file(self, source, _key, _content_type):
            FakeR2Storage.thumbnail_bytes = source.read_bytes()

        async def read_bytes(self, _key):
            return FakeR2Storage.thumbnail_bytes

    monkeypatch.setattr("app.media_routes.R2Storage", FakeR2Storage)
    monkeypatch.setattr("app.tasks.R2Storage", FakeR2Storage)
    monkeypatch.setattr("app.storage.R2Storage", FakeR2Storage)
    monkeypatch.setattr("app.tasks.sha256_file", lambda _path: "a" * 64)
    monkeypatch.setattr("app.tasks.probe_video", lambda _path: (12, 1080, 1920))
    monkeypatch.setattr("app.tasks.process_media.delay", lambda _media_id: None)

    def fake_thumbnail(_source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake-jpeg")

    monkeypatch.setattr("app.tasks.create_thumbnail", fake_thumbnail)
    initialized = client.post(
        "/v1/media",
        json={"filename": "clip.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()
    media_id = initialized["id"]
    client.post(
        f"/v1/media/{media_id}/parts/1/confirm",
        json={"part_number": 1, "etag": "fake-etag", "size_bytes": 8},
        headers=headers,
    )

    completed = client.post(f"/v1/media/{media_id}/complete", headers=headers)
    assert completed.status_code == 202, completed.text
    assert completed.json()["status"] == "processing"
    assert FakeR2Storage.completed is True

    asyncio.run(_process_media(media_id))
    ready = client.get(f"/v1/media/{media_id}", headers=headers)
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["thumbnail_url"]
    assert FakeR2Storage.thumbnail_bytes == b"fake-jpeg"
    assert FakeR2Storage.processing_path is not None
    assert not FakeR2Storage.processing_path.parent.exists()


def test_draft_allows_no_platform_but_publish_requires_one(client):
    auth = register(client, "empty-draft@example.com")
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect("empty-draft@example.com", platforms=("instagram",)))
    media = client.post(
        "/v1/media",
        json={"filename": "choose-later.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()
    asyncio.run(mark_media_ready(media["id"]))

    draft = client.post(
        "/v1/posts",
        json={"media_id": media["id"], "caption": "Choose destinations later", "versions": []},
        headers=headers,
    )
    assert draft.status_code == 201, draft.text
    assert draft.json()["versions"] == []

    publish = client.post(
        f"/v1/posts/{draft.json()['id']}/publish", json={"mode": "now"}, headers=headers
    )
    assert publish.status_code == 422
    assert publish.json()["code"] == "platform_required"


def test_draft_can_reference_uploading_media_but_cannot_publish_it(client):
    email = "uploading-draft@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("instagram",)))
    media = client.post(
        "/v1/media",
        json={"filename": "still-uploading.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()

    draft = client.post(
        "/v1/posts",
        json={"media_id": media["id"], "caption": "Save while uploading", "versions": []},
        headers=headers,
    )
    assert draft.status_code == 201, draft.text
    assert draft.json()["media"]["status"] == "uploading"

    publish = client.post(
        f"/v1/posts/{draft.json()['id']}/publish",
        json={"mode": "now"},
        headers=headers,
    )
    assert publish.status_code == 409
    assert publish.json()["code"] == "media_not_ready"


def test_draft_rejects_failed_media(client):
    email = "failed-media-draft@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email))
    media = client.post(
        "/v1/media",
        json={"filename": "failed.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()

    async def mark_media_failed() -> None:
        async with SessionLocal() as db:
            asset = await db.get(MediaAsset, media["id"])
            asset.status = "failed"
            await db.commit()

    asyncio.run(mark_media_failed())
    draft = client.post(
        "/v1/posts",
        json={"media_id": media["id"], "caption": "Should fail", "versions": []},
        headers=headers,
    )
    assert draft.status_code == 409
    assert draft.json()["code"] == "media_failed"


def test_retry_submits_only_the_failed_platform(client, monkeypatch):
    email = "platform-retry@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("instagram", "youtube")))
    media = client.post(
        "/v1/media",
        json={"filename": "retry.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()
    asyncio.run(mark_media_ready(media["id"]))
    draft = client.post(
        "/v1/posts",
        json={
            "media_id": media["id"],
            "title": "Retry one destination",
            "caption": "Platform-specific retry",
            "hashtags": ["Reverb"],
            "content_format_version": 2,
            "versions": [
                {
                    "platform": "instagram",
                    "caption": "Already live",
                    "options": {"separate_caption": True},
                },
                {
                    "platform": "youtube",
                    "caption": "Retry me",
                    "title": "Retry me",
                    "options": {"separate_caption": True},
                },
            ],
        },
        headers=headers,
    ).json()
    monkeypatch.setattr("app.posts.publish_post.delay", lambda *_args, **_kwargs: None)
    queued = client.post(
        f"/v1/posts/{draft['id']}/publish", json={"mode": "now"}, headers=headers
    )
    assert queued.status_code == 202

    async def mark_partial_failure():
        async with SessionLocal() as db:
            await db.execute(
                update(Publication)
                .where(Publication.post_id == draft["id"], Publication.platform == "instagram")
                .values(status="published")
            )
            await db.execute(
                update(Publication)
                .where(Publication.post_id == draft["id"], Publication.platform == "youtube")
                .values(status="failed", error_message="Provider failed")
            )
            post = await db.get(Post, draft["id"])
            post.status = "partially_published"
            await db.commit()

    asyncio.run(mark_partial_failure())
    monkeypatch.setattr("app.posts.retry_platform.delay", lambda *_args, **_kwargs: None)
    retry = client.post(
        f"/v1/posts/{draft['id']}/publications/youtube/retry", headers=headers
    )
    assert retry.status_code == 202, retry.text
    assert {
        item["platform"]: item["status"] for item in retry.json()["publications"]
    } == {"instagram": "published", "youtube": "queued"}

    captured = {}

    async def publish_one(_self, **kwargs):
        captured.update(kwargs)
        return {
            "request_id": "youtube-retry-request",
            "results": {"youtube": {"success": True, "url": "https://youtube.test/retried"}},
        }

    monkeypatch.setattr(UploadPostClient, "publish_video", publish_one)
    asyncio.run(_retry_platform(draft["id"], "youtube"))
    assert [version["platform"] for version in captured["versions"]] == ["youtube"]
    assert captured["versions"][0]["caption"] == "Retry me #Reverb"
    assert captured["versions"][0]["title"] == "Retry me"
    completed = client.get(f"/v1/posts/{draft['id']}", headers=headers).json()
    assert {item["platform"]: item["status"] for item in completed["publications"]} == {
        "instagram": "published",
        "youtube": "published",
    }
    youtube_result = next(
        item for item in completed["publications"] if item["platform"] == "youtube"
    )
    assert youtube_result["submitted_caption"] == "Retry me #Reverb"
    assert youtube_result["submitted_title"] == "Retry me"


def test_retry_reconciles_late_success_without_posting_again(client, monkeypatch):
    email = "retry-late-success@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("instagram",)))
    media = client.post(
        "/v1/media",
        json={"filename": "late-success.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()
    asyncio.run(mark_media_ready(media["id"]))
    draft = client.post(
        "/v1/posts",
        json={
            "media_id": media["id"],
            "caption": "Reconcile before retry",
            "versions": [{"platform": "instagram", "caption": "Reconcile before retry"}],
        },
        headers=headers,
    ).json()
    monkeypatch.setattr("app.posts.publish_post.delay", lambda *_args, **_kwargs: None)
    started = client.post(
        f"/v1/posts/{draft['id']}/publish", json={"mode": "now"}, headers=headers
    )
    assert started.status_code == 202

    async def mark_failed():
        async with SessionLocal() as db:
            post = await db.get(Post, draft["id"])
            post.status = "failed"
            post.provider_request_id = "original-instagram-request"
            publication = await db.scalar(
                select(Publication).where(Publication.post_id == draft["id"])
            )
            publication.status = "failed"
            publication.provider_request_id = "original-instagram-request"
            await db.commit()

    asyncio.run(mark_failed())
    monkeypatch.setattr("app.posts.retry_platform.delay", lambda *_args, **_kwargs: None)
    queued = client.post(
        f"/v1/posts/{draft['id']}/publications/instagram/retry", headers=headers
    )
    assert queued.status_code == 202

    async def remote_status(_self, **kwargs):
        assert kwargs["request_id"] == "original-instagram-request"
        return {
            "request_id": "original-instagram-request",
            "status": "completed",
            "results": [
                {
                    "platform": "instagram",
                    "status": "completed",
                    "success": True,
                    "post_url": "https://instagram.test/already-live",
                }
            ],
        }

    async def must_not_retry(*_args, **_kwargs):
        raise AssertionError("A remotely published post must not be retried")

    monkeypatch.setattr(UploadPostClient, "status", remote_status)
    monkeypatch.setattr(UploadPostClient, "retry", must_not_retry)
    monkeypatch.setattr(UploadPostClient, "publish_video", must_not_retry)
    asyncio.run(_retry_platform(draft["id"], "instagram"))

    completed = client.get(f"/v1/posts/{draft['id']}", headers=headers).json()
    publication = completed["publications"][0]
    assert publication["status"] == "published"
    assert publication["url"] == "https://instagram.test/already-live"


def test_retryable_provider_result_stays_active_without_posting_again(client, monkeypatch):
    email = "retryable-still-active@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("instagram",)))
    media = client.post(
        "/v1/media",
        json={"filename": "retryable.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()
    asyncio.run(mark_media_ready(media["id"]))
    draft = client.post(
        "/v1/posts",
        json={
            "media_id": media["id"],
            "caption": "Original request is still active",
            "versions": [
                {"platform": "instagram", "caption": "Original request is still active"}
            ],
        },
        headers=headers,
    ).json()
    monkeypatch.setattr("app.posts.publish_post.delay", lambda *_args, **_kwargs: None)
    assert client.post(
        f"/v1/posts/{draft['id']}/publish", json={"mode": "now"}, headers=headers
    ).status_code == 202

    async def mark_false_failure():
        async with SessionLocal() as db:
            post = await db.get(Post, draft["id"])
            post.status = "failed"
            post.provider_request_id = "active-instagram-request"
            publication = await db.scalar(
                select(Publication).where(Publication.post_id == draft["id"])
            )
            publication.status = "failed"
            publication.provider_request_id = "active-instagram-request"
            await db.commit()

    asyncio.run(mark_false_failure())
    monkeypatch.setattr("app.posts.retry_platform.delay", lambda *_args, **_kwargs: None)
    assert client.post(
        f"/v1/posts/{draft['id']}/publications/instagram/retry", headers=headers
    ).status_code == 202

    async def remote_status(_self, **_kwargs):
        return {
            "request_id": "active-instagram-request",
            "status": "processing",
            "results": [
                {
                    "platform": "instagram",
                    "status": "retryable",
                    "success": False,
                    "error": "Temporary platform error",
                }
            ],
        }

    async def must_not_retry(*_args, **_kwargs):
        raise AssertionError("A retryable provider request must remain the only live attempt")

    monkeypatch.setattr(UploadPostClient, "status", remote_status)
    monkeypatch.setattr(UploadPostClient, "retry", must_not_retry)
    monkeypatch.setattr(UploadPostClient, "publish_video", must_not_retry)
    asyncio.run(_retry_platform(draft["id"], "instagram"))

    current = client.get(f"/v1/posts/{draft['id']}", headers=headers).json()
    publication = current["publications"][0]
    assert publication["status"] == "publishing"
    assert publication["error_message"] is None


@pytest.mark.parametrize(("platform", "youtube_overflow"), [("instagram", False), ("x", False), ("linkedin", False), ("youtube", True)])
def test_confirmed_single_failure_uses_provider_retry(client, monkeypatch, youtube_overflow, platform):
    email = f"provider-retry-{platform}@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=(platform,)))
    media = client.post(
        "/v1/media",
        json={"filename": "provider-retry.mp4", "mime_type": "video/mp4", "size_bytes": 100000},
        headers=headers,
    ).json()
    asyncio.run(mark_media_ready(media["id"]))
    draft = client.post(
        "/v1/posts",
        json={
            "media_id": media["id"],
            "caption": "Use original request",
            "versions": [{"platform": platform, "caption": "Use original request", "title": "Valid title"}],
        },
        headers=headers,
    ).json()
    monkeypatch.setattr("app.posts.publish_post.delay", lambda *_args, **_kwargs: None)
    started = client.post(
        f"/v1/posts/{draft['id']}/publish", json={"mode": "now"}, headers=headers
    )
    assert started.status_code == 202

    async def mark_failed():
        async with SessionLocal() as db:
            post = await db.get(Post, draft["id"])
            post.status = "failed"
            post.provider_request_id = "failed-instagram-request"
            publication = await db.scalar(
                select(Publication).where(Publication.post_id == draft["id"])
            )
            publication.status = "failed"
            publication.provider_request_id = "failed-instagram-request"
            if youtube_overflow:
                # An old submitted snapshot must be checked even when the
                # current draft is valid. Provider retry reuses that snapshot.
                publication.submitted_caption = " ".join(["#repeat"] * 60)
                publication.submitted_title = "Title #one"
            await db.commit()

    asyncio.run(mark_failed())
    monkeypatch.setattr("app.posts.retry_platform.delay", lambda *_args, **_kwargs: None)
    queued = client.post(
        f"/v1/posts/{draft['id']}/publications/{platform}/retry", headers=headers
    )
    assert queued.status_code == 202

    async def remote_status(_self, **_kwargs):
        return {
            "request_id": "failed-instagram-request",
            "status": "failed",
            "results": [
                {
                    "platform": platform,
                    "status": "failed",
                    "success": False,
                    "error": "Provider failed",
                }
            ],
        }

    retried = {}

    async def provider_retry(_self, **kwargs):
        retried.update(kwargs)
        return {"success": True, "request_id": "failed-instagram-request"}

    async def must_not_upload(*_args, **_kwargs):
        raise AssertionError("Provider retry must reuse the original media")

    monkeypatch.setattr(UploadPostClient, "status", remote_status)
    monkeypatch.setattr(UploadPostClient, "retry", provider_retry)
    monkeypatch.setattr(UploadPostClient, "publish_video", must_not_upload)
    asyncio.run(_retry_platform(draft["id"], platform))

    completed = client.get(f"/v1/posts/{draft['id']}", headers=headers).json()
    if youtube_overflow:
        assert retried == {}
        assert completed["publications"][0]["status"] == "failed"
        assert completed["publications"][0]["error_code"] == "content_validation_failed"
        assert "61 hashtags" in completed["publications"][0]["error_message"]
        # This suite shares its database. Keep this test's failure notification
        # from being delivered by the notification tests later in the run.
        async def cancel_test_notification():
            async with SessionLocal() as db:
                await db.execute(
                    update(PostNotification)
                    .where(PostNotification.post_id == draft["id"])
                    .values(status="cancelled")
                )
                await db.commit()

        asyncio.run(cancel_test_notification())
    else:
        assert retried["request_id"] == "failed-instagram-request"
        assert completed["publications"][0]["status"] == "queued"


@pytest.mark.parametrize("mode", ["now", "scheduled"])
def test_publish_blocks_combined_youtube_hashtags_before_queueing(client, monkeypatch, mode):
    email = f"hashtag-{mode}@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("youtube",)))
    media = client.post("/v1/media", headers=headers, json={
        "filename": "hashtags.mp4", "mime_type": "video/mp4", "size_bytes": 8,
    }).json()
    asyncio.run(mark_media_ready(media["id"]))
    draft = client.post("/v1/posts", headers=headers, json={
        "media_id": media["id"], "content_format_version": 2,
        "caption": " ".join(["#repeat"] * 59), "hashtags": ["field"],
        "versions": [{"platform": "youtube", "title": "Title #one", "use_shared_caption": True}],
    })
    assert draft.status_code == 201
    queued = []
    monkeypatch.setattr("app.posts.publish_post.delay", lambda post_id: queued.append(post_id))
    payload = {"mode": mode}
    if mode == "scheduled":
        payload.update(scheduled_at=(datetime.now(UTC) + timedelta(minutes=20)).isoformat(), timezone="Africa/Lagos")
    result = client.post(f"/v1/posts/{draft.json()['id']}/publish", headers=headers, json=payload)
    assert result.status_code == 422
    assert "61 hashtags" in result.json()["field_errors"]["versions.youtube.caption"]
    assert queued == []
    assert client.get(f"/v1/posts/{draft.json()['id']}", headers=headers).json()["status"] == "draft"


def test_shared_schedule_creation_and_publish_validation(client, monkeypatch):
    auth = register(client, "schedule@example.com")
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect("schedule@example.com"))
    initialized = client.post(
        "/v1/media",
        json={"filename": "scheduled.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers={**headers},
    )
    media_id = initialized.json()["id"]
    asyncio.run(mark_media_ready(media_id))
    draft = client.post(
        "/v1/posts",
        json={
            "media_id": media_id,
            "title": "Scheduled launch",
            "caption": "Launch day",
            "hashtags": ["launch"],
            "versions": [{"platform": "tiktok", "caption": "Launch day #launch"}],
        },
        headers=headers,
    )
    assert draft.status_code == 201, draft.text
    monkeypatch.setattr("app.posts.publish_post.delay", lambda *_args, **_kwargs: None)
    scheduled_at = datetime.now(UTC) + timedelta(minutes=20)
    scheduled = client.post(
        f"/v1/posts/{draft.json()['id']}/publish",
        json={
            "mode": "scheduled",
            "scheduled_at": scheduled_at.isoformat(),
            "timezone": "Africa/Lagos",
        },
        headers=headers,
    )
    assert scheduled.status_code == 202, scheduled.text
    body = scheduled.json()
    assert body["status"] == "scheduled"
    assert body["schedule_timezone"] == "Africa/Lagos"
    assert {item["status"] for item in body["publications"]} == {"scheduled"}

    second = client.post(
        "/v1/posts",
        json={"media_id": media_id, "versions": [{"platform": "tiktok", "caption": ""}]},
        headers=headers,
    )
    assert second.status_code == 201
    rejected = client.post(
        f"/v1/posts/{second.json()['id']}/publish", json={"mode": "now"}, headers=headers
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "content_validation_failed"
    assert rejected.json()["field_errors"]["versions.tiktok.caption"] == "Add a caption"


def test_webhook_signature_and_deduplication(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "upload_post_webhook_secret", "webhook-test-secret")
    body = json.dumps({"event_id": "evt-api-1", "request_id": "unknown", "results": []}).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"webhook-test-secret", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    signed_headers = {
        "X-Upload-Post-Signature": f"sha256={signature}",
        "X-Upload-Post-Timestamp": timestamp,
        "X-Upload-Post-Delivery": "delivery-1",
        "Content-Type": "application/json",
    }
    first = client.post(
        "/v1/webhooks/upload-post",
        content=body,
        headers=signed_headers,
    )
    assert first.status_code == 200
    assert first.json()["message"] == "Processed"
    duplicate = client.post(
        "/v1/webhooks/upload-post",
        content=body,
        headers=signed_headers,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["message"] == "Already processed"
    rejected = client.post(
        "/v1/webhooks/upload-post",
        content=body,
        headers={
            "X-Upload-Post-Signature": "wrong",
            "X-Upload-Post-Timestamp": timestamp,
            "Content-Type": "application/json",
        },
    )
    assert rejected.status_code == 401


def test_webhook_applies_single_platform_result(client, monkeypatch):
    auth = register(client, "webhook-result@example.com")
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect("webhook-result@example.com"))
    media = client.post(
        "/v1/media",
        json={"filename": "result.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()
    asyncio.run(mark_media_ready(media["id"]))
    draft = client.post(
        "/v1/posts",
        json={
            "media_id": media["id"],
            "versions": [{"platform": "tiktok", "caption": "Published caption"}],
        },
        headers=headers,
    ).json()
    monkeypatch.setattr("app.posts.publish_post.delay", lambda *_args, **_kwargs: None)
    queued = client.post(
        f"/v1/posts/{draft['id']}/publish", json={"mode": "now"}, headers=headers
    )
    assert queued.status_code == 202

    monkeypatch.setattr(get_settings(), "upload_post_webhook_secret", "webhook-test-secret")
    payload = {
        "event": "upload_completed",
        "external_id": draft["id"],
        "platform": "tiktok",
        "result": {
            "success": True,
            "url": "https://www.tiktok.com/@creator/video/123",
            "publish_id": "123",
        },
    }
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"webhook-test-secret", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    delivered = client.post(
        "/v1/webhooks/upload-post",
        content=body,
        headers={
            "X-Upload-Post-Signature": f"sha256={signature}",
            "X-Upload-Post-Timestamp": timestamp,
            "X-Upload-Post-Delivery": "delivery-result-1",
            "Content-Type": "application/json",
        },
    )
    assert delivered.status_code == 200
    post = client.get(f"/v1/posts/{draft['id']}", headers=headers).json()
    assert post["status"] == "published"
    assert post["publications"][0]["url"].endswith("/123")
    assert post["publications"][0]["published_at"] is not None


def test_worker_database_connections_are_not_reused_across_event_loops():
    assert isinstance(task_engine.pool, NullPool)


def test_stale_unclaimed_post_is_redispatched(client, monkeypatch):
    email = "stale-queue@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("instagram",)))
    media = client.post(
        "/v1/media",
        json={"filename": "stale.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()
    asyncio.run(mark_media_ready(media["id"]))
    draft = client.post(
        "/v1/posts",
        json={
            "media_id": media["id"],
            "caption": "Recover this upload",
            "versions": [{"platform": "instagram", "caption": "Recover this upload"}],
        },
        headers=headers,
    ).json()
    monkeypatch.setattr("app.posts.publish_post.delay", lambda *_args, **_kwargs: None)
    assert client.post(
        f"/v1/posts/{draft['id']}/publish", json={"mode": "now"}, headers=headers
    ).status_code == 202

    async def age_post():
        async with SessionLocal() as db:
            post = await db.get(Post, draft["id"])
            post.updated_at = datetime.now(UTC) - timedelta(minutes=2)
            await db.commit()

    asyncio.run(age_post())
    redispatched: list[str] = []
    monkeypatch.setattr("app.tasks.publish_post.delay", lambda post_id: redispatched.append(post_id))
    monkeypatch.setattr("app.tasks.reconcile_post.delay", lambda *_args, **_kwargs: None)
    asyncio.run(_reconcile_active())
    assert redispatched == [draft["id"]]


def test_single_and_bulk_post_deletion(client):
    email = "delete-posts@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("instagram",)))
    media = client.post(
        "/v1/media",
        json={"filename": "delete.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()
    asyncio.run(mark_media_ready(media["id"]))

    def create_draft(caption: str) -> dict:
        response = client.post(
            "/v1/posts",
            json={
                "media_id": media["id"],
                "caption": caption,
                "versions": [{"platform": "instagram", "caption": caption}],
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    first = create_draft("Delete one")
    second = create_draft("Delete in a group")
    deleted = client.delete(f"/v1/posts/{first['id']}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/v1/posts/{first['id']}", headers=headers).status_code == 404

    bulk = client.post(
        "/v1/posts/bulk-delete",
        json={"post_ids": [second["id"], "missing-post"]},
        headers=headers,
    )
    assert bulk.status_code == 200, bulk.text
    assert bulk.json()["deleted_ids"] == [second["id"]]
    assert bulk.json()["failures"][0]["code"] == "post_not_found"

    async def media_has_retention_deadline() -> bool:
        async with SessionLocal() as db:
            asset = await db.get(MediaAsset, media["id"])
            return asset.delete_after is not None

    assert asyncio.run(media_has_retention_deadline())


def test_started_post_cannot_be_deleted(client, monkeypatch):
    email = "active-delete@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("instagram",)))
    media = client.post(
        "/v1/media",
        json={"filename": "active.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()
    asyncio.run(mark_media_ready(media["id"]))
    draft = client.post(
        "/v1/posts",
        json={
            "media_id": media["id"],
            "caption": "Publishing now",
            "versions": [{"platform": "instagram", "caption": "Publishing now"}],
        },
        headers=headers,
    ).json()
    monkeypatch.setattr("app.posts.publish_post.delay", lambda *_args, **_kwargs: None)
    client.post(f"/v1/posts/{draft['id']}/publish", json={"mode": "now"}, headers=headers)

    async def claim_post():
        async with SessionLocal() as db:
            publication = await db.scalar(
                select(Publication).where(Publication.post_id == draft["id"])
            )
            publication.attempts = 1
            await db.commit()

    asyncio.run(claim_post())
    response = client.delete(f"/v1/posts/{draft['id']}", headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "post_publishing"


@pytest.mark.parametrize("platform", ["x", "linkedin"])
def test_new_platform_connect_and_sync(client, monkeypatch, platform):
    email = f"new-connection-{platform}@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=()))
    pinned = True
    async def profile(_self, _username):
        return {"profile": {"social_accounts": {platform: {"username": "member", "display_name": "Personal Name"}}, "linkedin_page_id": "urn:li:organization:123" if pinned else None, "linkedin_page_name": "Our Company" if pinned else None}}
    async def connect(_self, _username, target, redirect_url):
        assert target == platform
        assert redirect_url.endswith(f"connect_platform={platform}")
        return "https://app.upload-post.com/connect?token=test"
    monkeypatch.setattr(UploadPostClient, "get_profile", profile)
    monkeypatch.setattr(UploadPostClient, "connection_access_url", connect)
    assert client.post(f"/v1/connections/{platform}/authorize", headers=headers).status_code == 200
    assert client.post(f"/v1/connections/{platform}/manage", headers=headers).status_code == 200
    response = client.post("/v1/connections/sync", headers=headers)
    assert response.status_code == 200
    item = next(a for a in response.json() if a["platform"] == platform)
    assert item["status"] == "connected"
    if platform == "linkedin":
        assert item["target_page_id"] == "urn:li:organization:123"
        assert item["display_name"] == "Our Company"
        pinned = False
        response = client.post("/v1/connections/sync", headers=headers)
        item = next(a for a in response.json() if a["platform"] == platform)
        assert item["target_page_id"] is None
        assert item["display_name"] == "Personal Name"


@pytest.mark.parametrize("mode", ["now", "scheduled"])
@pytest.mark.parametrize("platform", ["x", "linkedin"])
def test_new_video_destinations_publish_and_reject_bad_copy(client, monkeypatch, mode, platform):
    email = f"video-{platform}-{mode}@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=(platform,)))
    media = client.post("/v1/media", headers=headers, json={"filename": "clip.mp4", "mime_type": "video/mp4", "size_bytes": 100000}).json()
    asyncio.run(mark_media_ready(media["id"]))
    if platform == "x":
        async def oversized_video():
            async with SessionLocal() as db:
                asset = await db.get(MediaAsset, media["id"])
                asset.width, asset.height = 2160, 3840
                await db.commit()
        asyncio.run(oversized_video())
    data = {"media_id": media["id"], "caption": "a" * (281 if platform == "x" else 3001), "hashtags": [], "content_format_version": 2, "versions": [{"platform": platform, "use_shared_caption": True}]}
    draft = client.post("/v1/posts", headers=headers, json=data)
    assert draft.status_code == 201
    post_id = draft.json()["id"]
    queued = []
    monkeypatch.setattr("app.posts.publish_post.delay", lambda p: queued.append(p))
    payload = {"mode": mode}
    if mode == "scheduled":
        payload.update(scheduled_at=(datetime.now(UTC) + timedelta(minutes=20)).isoformat(), timezone="Africa/Lagos")
    response = client.post(f"/v1/posts/{post_id}/publish", headers=headers, json=payload)
    assert response.status_code == 422
    assert queued == []
    data["caption"] = "Valid caption #video"
    if platform == "x":
        data["hashtags"] = [f"LongTag{i}" + "a" * 40 for i in range(10)]
        data["versions"][0]["options"] = {
            "ai_hashtags": [data["hashtags"][0]], "ai_hashtags_source": data["hashtags"],
        }
    assert client.patch(f"/v1/posts/{post_id}", headers=headers, json=data).status_code == 200
    response = client.post(f"/v1/posts/{post_id}/publish", headers=headers, json=payload)
    assert response.status_code == 202, response.text
    assert queued == [post_id]


@pytest.fixture(autouse=True)
def empty_upload_history(monkeypatch):
    async def history(_self, **_kwargs):
        return {"history": [], "in_progress": []}
    monkeypatch.setattr(UploadPostClient, "upload_history", history)


def retry_fixture(client, monkeypatch, suffix):
    email = f"retry-regression-{suffix}@example.com".lower()
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("facebook", "youtube")))
    media = client.post("/v1/media", headers=headers, json={
        "filename": "retry.mp4", "mime_type": "video/mp4", "size_bytes": 8,
    }).json()
    asyncio.run(mark_media_ready(media["id"]))
    post = client.post("/v1/posts", headers=headers, json={
        "media_id": media["id"], "caption": "Retry safely",
        "versions": [{"platform": p, "caption": "Retry safely", "title": "Retry safely"} for p in ("facebook", "youtube")],
    }).json()
    monkeypatch.setattr("app.posts.publish_post.delay", lambda *_: None)
    assert client.post(f"/v1/posts/{post['id']}/publish", headers=headers, json={"mode": "now"}).status_code == 202

    async def seed():
        async with SessionLocal() as db:
            obj = await db.get(Post, post['id'])
            obj.status = "partially_published"
            obj.provider_request_id, obj.provider_job_id = f"old-request-{obj.id}", f"old-job-{obj.id}"
            rows = (await db.scalars(select(Publication).where(Publication.post_id == obj.id))).all()
            for row in rows:
                row.provider_request_id, row.provider_job_id = f"old-request-{obj.id}", f"old-job-{obj.id}"
                row.status = "failed" if row.platform == "facebook" else "published"
            await db.commit()
    asyncio.run(seed())
    monkeypatch.setattr("app.posts.retry_platform.delay", lambda *_: None)
    return post['id'], headers


async def publication_state(post_id):
    from app.models import PublicationAttempt
    async with SessionLocal() as db:
        row = await db.scalar(select(Publication).where(Publication.post_id == post_id, Publication.platform == "facebook"))
        attempts = (await db.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == row.id))).all()
        return {"status": row.status, "request_id": row.provider_request_id, "job_id": row.provider_job_id,
                "post_id": row.provider_post_id, "url": row.url,
                "attempts": [(a.provider_request_id, a.provider_job_id) for a in attempts]}


@pytest.mark.parametrize("new_request,new_job", [("new-request", None), ("new-request", "new-job"), (None, "job-only")])
def test_retry_tracks_new_reference_and_ignores_old_failure(client, monkeypatch, new_request, new_job):
    from app.tasks import _reconcile_post
    post_id, headers = retry_fixture(client, monkeypatch, f"new-id-{new_job}")
    calls = []

    async def status(_self, **kwargs):
        calls.append(kwargs)
        new = kwargs['request_id'] == 'new-request' or kwargs['job_id'] == 'job-only'
        return {"request_id": kwargs['request_id'], "results": [{
            "platform": "facebook", "success": new,
            "platform_post_id": "123" if new else None,
            "post_url": "https://www.facebook.com/reel/123" if new else None,
            "error_message": None if new else "No Facebook Page",
        }]}

    async def retry(_self, **kwargs):
        return {"success": True, "request_id": new_request, "job_id": new_job}
    monkeypatch.setattr(UploadPostClient, 'status', status)
    monkeypatch.setattr(UploadPostClient, 'retry', retry)
    assert client.post(f'/v1/posts/{post_id}/publications/facebook/retry', headers=headers).status_code == 202
    # Beat can run before the queued worker; it must not resurrect the old failure.
    asyncio.run(_reconcile_post(post_id))
    assert calls == []
    asyncio.run(_retry_platform(post_id, 'facebook'))
    saved = asyncio.run(publication_state(post_id))
    assert saved['request_id'] == new_request
    assert saved['job_id'] == new_job
    assert saved['attempts'] == [(new_request, new_job)]
    asyncio.run(_reconcile_post(post_id))
    assert calls[-1] == {'request_id': new_request, 'job_id': new_job}
    saved = asyncio.run(publication_state(post_id))
    assert saved['status'] == 'published'
    assert saved['post_id'] == '123'
    # A delayed failure callback from the original job cannot undo success.
    secret = 'retry-regression-secret'
    monkeypatch.setattr(get_settings(), 'upload_post_webhook_secret', secret)
    body = json.dumps({'external_id': post_id, 'request_id': f'old-request-{post_id}', 'job_id': f'old-job-{post_id}',
                       'platform': 'facebook', 'result': {'success': False, 'error': 'Old failure'}}).encode()
    ts = str(int(time.time()))
    signature = hmac.new(secret.encode(), ts.encode()+b'.'+body, hashlib.sha256).hexdigest()
    assert client.post('/v1/webhooks/upload-post', content=body, headers={
        'Content-Type':'application/json', 'X-Upload-Post-Timestamp':ts,
        'X-Upload-Post-Signature':signature, 'X-Upload-Post-Delivery':f'old-failure-{post_id}',
    }).status_code == 200
    assert asyncio.run(publication_state(post_id))['status'] == 'published'
    assert client.post(f'/v1/posts/{post_id}/publications/facebook/retry', headers=headers).status_code == 409


@pytest.mark.parametrize('path', ['retry', 'reconcile', 'missing_status'])
def test_history_recovers_lost_retry_without_reposting(client, monkeypatch, path):
    from app.providers import ProviderError
    from app.tasks import _reconcile_post
    post_id, headers = retry_fixture(client, monkeypatch, f'history-{path}')

    async def history(_self, **kwargs):
        return {'history': [{'external_id': post_id, 'platform': 'facebook', 'success': True,
                             'request_id': 'recovered-request', 'job_id': 'recovered-job',
                             'platform_post_id': '456', 'post_url': 'https://www.facebook.com/reel/456'}]}
    async def status(_self, **kwargs):
        if path == 'missing_status':
            raise ProviderError('provider_http_404', 'Not found', 404)
        return {'request_id':f'old-request-{post_id}','results':[{'platform':'facebook','success':False}]}
    async def no_upload(*args, **kwargs):
        raise AssertionError('Already live: must not submit another upload')
    monkeypatch.setattr(UploadPostClient, 'upload_history', history)
    monkeypatch.setattr(UploadPostClient, 'status', status)
    monkeypatch.setattr(UploadPostClient, 'retry', no_upload)
    monkeypatch.setattr(UploadPostClient, 'publish_video', no_upload)
    if path == 'retry':
        client.post(f'/v1/posts/{post_id}/publications/facebook/retry', headers=headers)
        asyncio.run(_retry_platform(post_id, 'facebook'))
    else:
        asyncio.run(_reconcile_post(post_id))
    saved = asyncio.run(publication_state(post_id))
    assert saved['status'] == 'published'
    assert saved['request_id'] == 'recovered-request'
    assert saved['job_id'] == 'recovered-job'
    assert saved['post_id'] == '456'


@pytest.mark.parametrize('history_mode', ['unavailable', 'in_progress'])
def test_uncertain_history_never_posts_again(client, monkeypatch, history_mode):
    from app.providers import ProviderError
    post_id, headers = retry_fixture(client, monkeypatch, f'uncertain-{history_mode}')
    async def history(_self, **kwargs):
        if history_mode == 'unavailable':
            raise ProviderError('provider_unavailable', 'Unavailable', 503)
        return {'history': [], 'in_progress': [{'external_id':post_id}]}
    async def no_upload(*args, **kwargs):
        raise AssertionError('Cannot safely retry while the result is unknown')
    monkeypatch.setattr(UploadPostClient, 'upload_history', history)
    monkeypatch.setattr(UploadPostClient, 'retry', no_upload)
    monkeypatch.setattr(UploadPostClient, 'publish_video', no_upload)
    client.post(f'/v1/posts/{post_id}/publications/facebook/retry', headers=headers)
    asyncio.run(_retry_platform(post_id, 'facebook'))
    assert asyncio.run(publication_state(post_id))['status'] == 'publishing'
    assert client.post(f'/v1/posts/{post_id}/publications/facebook/retry', headers=headers).status_code == 409


def test_current_failure_webhook_is_verified_before_exposing_retry(client, monkeypatch):
    from app.tasks import _reconcile_post
    post_id, headers = retry_fixture(client, monkeypatch, 'webhook-verification')
    secret = 'failure-verification-secret'
    monkeypatch.setattr(get_settings(), 'upload_post_webhook_secret', secret)
    body = json.dumps({'external_id':post_id, 'request_id':f'old-request-{post_id}', 'job_id':f'old-job-{post_id}',
                       'platform':'facebook', 'result':{'success':False, 'error':'Failed'}}).encode()
    ts = str(int(time.time()))
    signature = hmac.new(secret.encode(), ts.encode()+b'.'+body, hashlib.sha256).hexdigest()
    assert client.post('/v1/webhooks/upload-post', content=body, headers={
        'Content-Type':'application/json', 'X-Upload-Post-Timestamp':ts,
        'X-Upload-Post-Signature':signature, 'X-Upload-Post-Delivery':f'failure-{post_id}',
    }).status_code == 200
    assert asyncio.run(publication_state(post_id))['status'] == 'publishing'
    assert client.post(f'/v1/posts/{post_id}/publications/facebook/retry', headers=headers).status_code == 409
    async def status(_self, **kwargs):
        return {'request_id':f'old-request-{post_id}','results':[{'platform':'facebook','success':False,'error_message':'Still failed'}]}
    monkeypatch.setattr(UploadPostClient, 'status', status)
    asyncio.run(_reconcile_post(post_id))
    # No success or pending upload in history: the failure can now be shown.
    assert asyncio.run(publication_state(post_id))['status'] == 'failed'
    async def clean_notification():
        async with SessionLocal() as db:
            await db.execute(update(PostNotification).where(PostNotification.post_id == post_id).values(status='cancelled'))
            await db.commit()
    asyncio.run(clean_notification())


async def test_stale_failure_does_not_demote_new_retry_or_clear_saved_post_id():
    from types import SimpleNamespace

    from app.tasks import apply_provider_payload
    pub = SimpleNamespace(platform='facebook', status='queued', provider_request_id='new', provider_job_id='new-job',
                          provider_post_id=None, url=None, error_code=None, error_message=None,
                          fallback_to_inbox=False, published_at=None)
    post = SimpleNamespace(publications=[pub], status='publishing', media=SimpleNamespace(delete_after=None))
    await apply_provider_payload(post, {'request_id':'old','job_id':'old-job',
                                      'results':[{'platform':'facebook','success':False}]})
    assert pub.status == 'queued'
    await apply_provider_payload(post, {'request_id':'new','job_id':'new-job',
                                      'results':[{'platform':'facebook','success':True,'platform_post_id':'123'}]})
    await apply_provider_payload(post, {'request_id':'new','results':[{'platform':'facebook','success':True}]})
    assert pub.status == 'published'
    assert pub.provider_post_id == '123'


async def test_old_callback_cannot_cancel_a_retry_waiting_for_its_worker():
    from types import SimpleNamespace

    from app.tasks import apply_provider_payload
    pub = SimpleNamespace(platform='facebook', status='queued', provider_request_id='old', provider_job_id='old-job',
                          error_code='retry_pending')
    post = SimpleNamespace(publications=[pub], status='publishing', media=SimpleNamespace(delete_after=None))
    await apply_provider_payload(post, {'request_id':'old','job_id':'old-job',
                                      'results':[{'platform':'facebook','success':False}]})
    assert pub.status == 'queued'
    assert pub.error_code == 'retry_pending'
