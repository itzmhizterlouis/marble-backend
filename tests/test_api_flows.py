import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import Text, select, update
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.database import SessionLocal, task_engine
from app.models import MediaAsset, Post, Publication, SocialConnection, User
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

    asyncio.run(seed_connection())
    secret = "webhook-handle-secret"
    monkeypatch.setattr(get_settings(), "upload_post_webhook_secret", secret)
    payload = json.dumps(
        {
            "event": "social_account_connected",
            "event_id": "connection-handle-webhook-1",
            "profile_username": "marble_connection-webhook-handle",
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
            "versions": [
                {"platform": "instagram", "caption": "Already live"},
                {"platform": "youtube", "caption": "Retry me", "title": "Retry me"},
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
    completed = client.get(f"/v1/posts/{draft['id']}", headers=headers).json()
    assert {item["platform"]: item["status"] for item in completed["publications"]} == {
        "instagram": "published",
        "youtube": "published",
    }


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


def test_confirmed_single_failure_uses_provider_retry(client, monkeypatch):
    email = "provider-retry@example.com"
    auth = register(client, email)
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    asyncio.run(verify_and_connect(email, platforms=("instagram",)))
    media = client.post(
        "/v1/media",
        json={"filename": "provider-retry.mp4", "mime_type": "video/mp4", "size_bytes": 8},
        headers=headers,
    ).json()
    asyncio.run(mark_media_ready(media["id"]))
    draft = client.post(
        "/v1/posts",
        json={
            "media_id": media["id"],
            "caption": "Use original request",
            "versions": [{"platform": "instagram", "caption": "Use original request"}],
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
            await db.commit()

    asyncio.run(mark_failed())
    monkeypatch.setattr("app.posts.retry_platform.delay", lambda *_args, **_kwargs: None)
    queued = client.post(
        f"/v1/posts/{draft['id']}/publications/instagram/retry", headers=headers
    )
    assert queued.status_code == 202

    async def remote_status(_self, **_kwargs):
        return {
            "request_id": "failed-instagram-request",
            "status": "failed",
            "results": [
                {
                    "platform": "instagram",
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
    asyncio.run(_retry_platform(draft["id"], "instagram"))

    assert retried["request_id"] == "failed-instagram-request"
    completed = client.get(f"/v1/posts/{draft['id']}", headers=headers).json()
    assert completed["publications"][0]["status"] == "queued"


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
    assert rejected.json()["code"] == "caption_required"


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
