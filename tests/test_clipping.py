import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import delete, select

from app.config import get_settings
from app.database import SessionLocal
from app.models import FeatureFlag, MediaAsset, User


@pytest.fixture(autouse=True)
def isolate_clipping_records(client):
    from app.models import ClippingAccess, ClippingProject, GeneratedClip
    async def clear():
        async with SessionLocal() as db:
            for model in (GeneratedClip, ClippingProject, ClippingAccess):
                await db.execute(delete(model))
            await db.commit()
    asyncio.run(clear())


def creator(client):
    email = f"clips-{uuid.uuid4().hex[:12]}@example.com"
    auth = client.post("/v1/auth/register", json={
        "email": email, "password": "creator123", "name": "Clip Creator",
    }).json()

    async def verify():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == email))
            user.email_verified = True
            await db.commit()
            return user.id

    return asyncio.run(verify()), {"Authorization": f"Bearer {auth['access_token']}"}


def source(user_id, **overrides):
    async def save():
        async with SessionLocal() as db:
            values = dict(user_id=user_id, original_name="podcast.mp4", mime_type="video/mp4",
                          size_bytes=100, uploaded_bytes=100, storage_path="", storage_backend="r2",
                          object_key=f"clips/{uuid.uuid4()}.mp4", status="ready", duration_seconds=1800,
                          width=1920, height=1080, delete_after=datetime.now(UTC) + timedelta(days=7))
            values.update(overrides)
            asset = MediaAsset(**values)
            db.add(asset)
            await db.commit()
            return asset.id
    return asyncio.run(save())


def test_clipping_status_is_private_and_disabled_by_default(client, monkeypatch):
    if hasattr(get_settings(), "clipping_enabled"):
        monkeypatch.setattr(get_settings(), "clipping_enabled", False)
    _, headers = creator(client)
    assert client.get("/v1/clips/access").status_code == 401
    response = client.get("/v1/clips/access", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    assert client.get("/v1/clips/projects", headers=headers).status_code == 403


def test_clipping_requires_both_global_switch_and_individual_grant(client, monkeypatch):
    if hasattr(get_settings(), "clipping_enabled"):
        monkeypatch.setattr(get_settings(), "clipping_enabled", True)
        monkeypatch.setattr(get_settings(), "vizard_api_key", "test-key")
    _, headers = creator(client)

    async def enable():
        async with SessionLocal() as db:
            flag = await db.get(FeatureFlag, "clipping")
            if not flag:
                flag = FeatureFlag(key="clipping")
                db.add(flag)
            flag.enabled = True
            await db.commit()
    asyncio.run(enable())
    assert client.get("/v1/clips/access", headers=headers).json()["enabled"] is False
    assert client.post("/v1/clips/projects", headers=headers, json={"media_id": "anything"}).status_code == 403


def enable_creator(client, monkeypatch, minutes=60):
    monkeypatch.setattr(get_settings(), "clipping_enabled", True)
    monkeypatch.setattr(get_settings(), "vizard_api_key", "test-vizard-key")
    monkeypatch.setattr(get_settings(), "storage_backend", "r2")
    monkeypatch.setattr(get_settings(), "r2_endpoint", "https://test.r2.cloudflarestorage.com")
    monkeypatch.setattr(get_settings(), "r2_bucket_name", "test-bucket")
    monkeypatch.setattr(get_settings(), "r2_access_key_id", "test-access")
    monkeypatch.setattr(get_settings(), "r2_secret_access_key", "test-secret")
    user_id, headers = creator(client)
    from app.models import ClippingAccess

    async def grant():
        async with SessionLocal() as db:
            flag = await db.get(FeatureFlag, "clipping")
            if not flag:
                flag = FeatureFlag(key="clipping")
                db.add(flag)
            flag.enabled = True
            db.add(ClippingAccess(user_id=user_id, enabled=True, monthly_minutes=minutes))
            await db.commit()
    asyncio.run(grant())
    calls = []
    monkeypatch.setattr("app.clipping.dispatch_project", lambda project_id, **kwargs: calls.append(project_id))
    return user_id, headers, calls


def test_project_creation_is_owned_deduplicated_and_reserves_minutes(client, monkeypatch):
    user_id, headers, calls = enable_creator(client, monkeypatch)
    media_id = source(user_id, purpose="clipping")
    first = client.post("/v1/clips/projects", headers=headers, json={"media_id": media_id})
    assert first.status_code == 202, first.text
    duplicate = client.post("/v1/clips/projects", headers=headers, json={"media_id": media_id})
    assert duplicate.json()["id"] == first.json()["id"]
    assert calls == [first.json()["id"]]
    assert client.get("/v1/clips/access", headers=headers).json()["used_minutes"] == 30
    too_long = source(user_id, purpose="clipping", duration_seconds=3600)
    denied = client.post("/v1/clips/projects", headers=headers, json={"media_id": too_long})
    assert denied.status_code == 409
    assert len(calls) == 1
    other, other_headers, _ = enable_creator(client, monkeypatch)
    assert other != user_id
    assert client.get(f"/v1/clips/projects/{first.json()['id']}", headers=other_headers).status_code == 404
    assert client.post("/v1/clips/projects", headers=other_headers, json={"media_id": media_id}).status_code == 404


def test_long_sources_cannot_be_used_as_normal_posts(client, monkeypatch):
    user_id, headers, _ = enable_creator(client, monkeypatch)
    media_id = source(user_id, purpose="clipping")
    denied = client.post("/v1/posts", headers=headers, json={"media_id": media_id, "caption": "Source"})
    assert denied.status_code == 409
    assert denied.json()["code"] == "clipping_source_not_publishable"


def test_clipping_upload_limit_does_not_raise_normal_post_limit(client, monkeypatch):
    _, headers, _ = enable_creator(client, monkeypatch)
    async def multipart(self, key, content_type):
        return "test-upload"
    monkeypatch.setattr("app.media_routes.R2Storage.create_multipart_upload", multipart)
    payload = {"filename": "long.mp4", "mime_type": "video/mp4", "size_bytes": 600 * 1024 * 1024}
    assert client.post("/v1/media", headers=headers, json=payload).status_code == 413
    accepted = client.post("/v1/media", headers=headers, json={**payload, "purpose": "clipping"})
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["purpose"] == "clipping"
    oversized = client.post("/v1/media", headers=headers, json={**payload, "purpose": "clipping", "size_bytes": 2147483649})
    assert oversized.status_code == 413


def test_one_clip_creates_one_draft_and_cannot_leak_to_another_creator(client, monkeypatch):
    user_id, headers, _ = enable_creator(client, monkeypatch)
    media_id = source(user_id, purpose="clipping")
    project = client.post("/v1/clips/projects", headers=headers, json={"media_id": media_id}).json()
    from app.models import GeneratedClip

    async def save_clip():
        async with SessionLocal() as db:
            output = MediaAsset(user_id=user_id, original_name="short.mp4", mime_type="video/mp4", size_bytes=10,
                                uploaded_bytes=10, storage_backend="r2", object_key="outputs/short.mp4", storage_path="",
                                status="ready", duration_seconds=45, width=1080, height=1920)
            db.add(output)
            await db.flush()
            clip = GeneratedClip(project_id=project["id"], user_id=user_id, provider_clip_id="123", media_id=output.id,
                                 title="A complete thought", status="ready")
            db.add(clip)
            await db.commit()
            return clip.id
    clip_id = asyncio.run(save_clip())
    a = client.post(f"/v1/clips/items/{clip_id}/draft", headers=headers)
    b = client.post(f"/v1/clips/items/{clip_id}/draft", headers=headers)
    assert a.status_code == b.status_code == 200, a.text
    assert a.json()["id"] == b.json()["id"]
    assert a.json()["media"]["duration_seconds"] == 45
    assert len(client.get("/v1/posts", headers=headers).json()["items"]) == 1
    _, other_headers, _ = enable_creator(client, monkeypatch)
    assert client.post(f"/v1/clips/items/{clip_id}/draft", headers=other_headers).status_code == 404
    assert client.delete(f"/v1/clips/items/{clip_id}", headers=headers).status_code == 409


@pytest.mark.asyncio
async def test_vizard_contract_and_uncertain_submission_are_distinct(monkeypatch):
    from app.vizard import VizardClient, VizardError
    monkeypatch.setattr(get_settings(), "vizard_api_key", "test-key")
    endpoint = "https://elb-api.vizard.ai/hvizard-server-front/open-api/v1/project/create"
    with respx.mock as mock:
        route = mock.post(endpoint).mock(return_value=httpx.Response(200, json={"code": 2000, "projectId": 35613542}))
        result = await VizardClient().submit("https://storage.example.com/video.mp4", {"length": "short", "style": "clean", "subtitles": True, "keywords": "Practical advice", "remove_silence": False}, "Reverb:test", "mp4")
        assert result == "35613542"
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["preferLength"] == [2]
        assert body["videoType"] == 1 and body["ratioOfClip"] == 1
        assert body["keywords"] == "Practical advice" and body["subtitleSwitch"] == 1
        assert "keyword" not in body and body["ext"] == "mp4"
        assert route.calls[0].request.headers["VIZARDAI_API_KEY"] == "test-key"
        route.mock(side_effect=httpx.ReadTimeout("timeout"))
        with pytest.raises(VizardError) as error:
            await VizardClient().submit("https://storage.example.com/video.mp4", {}, "Reverb:test", "mp4")
        assert error.value.uncertain is True
        route.mock(return_value=httpx.Response(200, json={"code": 4007, "errMsg": "PRIVATE DETAILS"}))
        with pytest.raises(VizardError) as error:
            await VizardClient().submit("https://storage.example.com/video.mp4", {}, "Reverb:test", "mp4")
        assert error.value.uncertain is False
        assert "PRIVATE" not in str(error.value)


def test_uncertain_worker_submission_is_never_repeated(client, monkeypatch):
    from app.clipping_worker import run_project
    from app.models import ClippingProject
    user_id, headers, _ = enable_creator(client, monkeypatch)
    media_id = source(user_id, purpose="clipping")
    project = client.post("/v1/clips/projects", headers=headers, json={"media_id": media_id}).json()

    async def signed(self, key, **kwargs):
        return "https://storage.example.com/source.mp4"
    monkeypatch.setattr("app.clipping_worker.R2Storage.presign_download", signed)
    with respx.mock as mock:
        route = mock.post("https://elb-api.vizard.ai/hvizard-server-front/open-api/v1/project/create").mock(side_effect=httpx.ReadTimeout("lost response"))
        asyncio.run(run_project(project["id"]))
        asyncio.run(run_project(project["id"]))
        assert len(route.calls) == 1

    async def read():
        async with SessionLocal() as db:
            row = await db.get(ClippingProject, project["id"])
            assert row.status == "uncertain"
            assert row.billing_status == "submitted"
    asyncio.run(read())


@pytest.mark.asyncio
async def test_output_download_rejects_private_hosts_and_excess_bytes(tmp_path, monkeypatch):
    from app.vizard import VizardClient, VizardError
    monkeypatch.setattr(get_settings(), "vizard_api_key", "test-key")
    with respx.mock as mock:
        with pytest.raises(VizardError):
            await VizardClient().download("http://127.0.0.1/private", tmp_path / "unsafe.mp4", 100)
        mock.get("https://cdn-video.vizard.ai/clip.mp4").mock(return_value=httpx.Response(200, content=b"123456"))
        with pytest.raises(VizardError):
            await VizardClient().download("https://cdn-video.vizard.ai/clip.mp4", tmp_path / "oversized.mp4", 5)
        assert not (tmp_path / "unsafe.mp4").exists()


def test_global_budget_is_reserved_across_creators(client, monkeypatch):
    first_user, first_headers, _ = enable_creator(client, monkeypatch)
    second_user, second_headers, _ = enable_creator(client, monkeypatch)
    monkeypatch.setattr(get_settings(), "clipping_monthly_credits", 30)
    one = source(first_user, purpose="clipping")
    two = source(second_user, purpose="clipping")
    assert client.post("/v1/clips/projects", headers=first_headers, json={"media_id": one}).status_code == 202
    response = client.post("/v1/clips/projects", headers=second_headers, json={"media_id": two})
    assert response.status_code == 409
    assert response.json()["code"] == "clipping_capacity_reached"


def test_worker_imports_once_and_retry_only_queries_existing_project(client, monkeypatch):
    from app.clipping_worker import run_project
    user_id, headers, _ = enable_creator(client, monkeypatch)
    media_id = source(user_id, purpose="clipping")
    project = client.post("/v1/clips/projects", headers=headers, json={"media_id": media_id}).json()

    async def signed(self, key, **kwargs):
        return "https://storage.example.com/source.mp4"
    async def put_file(self, path, key, content_type):
        assert path.is_file()
    def thumbnail(path, output):
        output.write_bytes(b"jpeg")
    monkeypatch.setattr("app.clipping_worker.R2Storage.presign_download", signed)
    monkeypatch.setattr("app.clipping_worker.R2Storage.put_file", put_file)
    monkeypatch.setattr("app.clipping_worker.probe_video", lambda path: (45, 1080, 1920))
    monkeypatch.setattr("app.clipping_worker.create_thumbnail", thumbnail)
    with respx.mock as mock:
        submit = mock.post("https://elb-api.vizard.ai/hvizard-server-front/open-api/v1/project/create").mock(return_value=httpx.Response(200, json={"code": 2000, "projectId": 12345}))
        query = mock.get("https://elb-api.vizard.ai/hvizard-server-front/open-api/v1/project/query/12345").mock(return_value=httpx.Response(200, json={"code": 2000, "projectId": 12345, "videos": [{"videoId": 55, "title": "A complete story", "videoUrl": "https://cdn-video.vizard.ai/export.mp4"}]}))
        download = mock.get("https://cdn-video.vizard.ai/export.mp4").mock(return_value=httpx.Response(200, content=b"video"))
        asyncio.run(run_project(project["id"]))
        asyncio.run(run_project(project["id"]))
        ready = client.get(f"/v1/clips/projects/{project['id']}", headers=headers).json()
        assert ready["status"] == "ready"
        assert ready["clips"][0]["media"]["baked_subtitles"] is True
        assert ready["clips"][0]["media"]["purpose"] == "post"
        assert "videoUrl" not in str(ready) and "storage.example.com" not in str(ready)
        assert client.post(f"/v1/clips/projects/{project['id']}/retry", headers=headers).status_code == 202
        asyncio.run(run_project(project["id"]))
        assert len(submit.calls) == len(download.calls) == 1
        assert len(query.calls) == 2


def test_admin_grants_require_owner_and_are_audited(client, monkeypatch):
    _, headers = creator(client)
    target_id, _ = creator(client)
    body = {"enabled": True, "monthly_minutes": 120, "reason": "Private beta invitation"}
    path = f"/v1/admin/clipping/users/{target_id}"
    assert client.put(path, json=body, headers=headers).status_code == 403
    email = client.get("/v1/auth/me", headers=headers).json()["email"]
    monkeypatch.setattr(get_settings(), "admin_emails", email)
    response = client.put(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": True, "monthly_minutes": 120}
    assert client.get("/v1/admin/users", headers=headers).json()["items"][0]["clipping_enabled"] is True
