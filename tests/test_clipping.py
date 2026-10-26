import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import FeatureFlag, MediaAsset, User


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
