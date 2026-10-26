"""CI-only PostgreSQL checks for clipping reservation/draft races after migrations."""
import asyncio
import uuid

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import ClippingAccess, ClippingProject, FeatureFlag, GeneratedClip, MediaAsset, Post, User
from app.security import create_access_token


async def check():
    settings = get_settings()
    settings.clipping_enabled, settings.vizard_api_key, settings.storage_backend = True, "ci-only", "r2"
    settings.clipping_monthly_credits = 30
    import app.clipping as clipping
    calls = []
    clipping.dispatch_project = lambda project_id, **kwargs: calls.append(project_id)
    records = []
    async with SessionLocal() as db:
        flag = await db.get(FeatureFlag, "clipping")
        assert flag is not None and not flag.enabled
        flag.enabled = True
        for _ in range(2):
            unique = uuid.uuid4().hex
            user = User(email=f"clip-race-{unique}@example.com", name="Clip Race", email_verified=True, upload_post_profile=unique)
            db.add(user)
            await db.flush()
            db.add(ClippingAccess(user_id=user.id, enabled=True, monthly_minutes=60))
            media = MediaAsset(user_id=user.id, original_name="source.mp4", mime_type="video/mp4", size_bytes=10, uploaded_bytes=10,
                               storage_path="", storage_backend="r2", object_key=f"ci/{unique}.mp4", purpose="clipping",
                               status="ready", duration_seconds=1800, width=1920, height=1080)
            db.add(media)
            await db.flush()
            records.append((user.id, media.id, {"Authorization": f"Bearer {create_access_token(user.id)}"}))
        await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test.example") as client:
        results = await asyncio.gather(*(client.post("/v1/clips/projects", json={"media_id": media}, headers=headers) for _, media, headers in records))
        assert sorted(response.status_code for response in results) == [202, 409], [response.text for response in results]
        winner = next(index for index, response in enumerate(results) if response.status_code == 202)
        user_id, media_id, headers = records[winner]
        project_id = results[winner].json()["id"]
        duplicate = await asyncio.gather(*(client.post("/v1/clips/projects", json={"media_id": media_id}, headers=headers) for _ in range(6)))
        assert all(response.status_code == 202 and response.json()["id"] == project_id for response in duplicate)
        assert calls == [project_id]
        async with SessionLocal() as db:
            project = await db.get(ClippingProject, project_id)
            assert project.reserved_credit_milli == 30000
            output = MediaAsset(user_id=user_id, original_name="clip.mp4", mime_type="video/mp4", size_bytes=10, uploaded_bytes=10,
                                storage_path="", storage_backend="r2", object_key="ci/output.mp4", status="ready", duration_seconds=45,
                                width=1080, height=1920)
            db.add(output)
            await db.flush()
            clip = GeneratedClip(user_id=user_id, project_id=project_id, provider_clip_id="123", media_id=output.id,
                                 title="Race-safe draft", status="ready")
            db.add(clip)
            await db.commit()
            clip_id = clip.id
        drafts = await asyncio.gather(*(client.post(f"/v1/clips/items/{clip_id}/draft", headers=headers) for _ in range(6)))
        assert all(response.status_code == 200 for response in drafts), [response.text for response in drafts]
        assert len({response.json()["id"] for response in drafts}) == 1
        async with SessionLocal() as db:
            posts = list(await db.scalars(select(Post).where(Post.user_id == user_id)))
            assert len(posts) == 1
    print("Clipping migration, global reservation, duplicate submission and draft races passed")


if __name__ == "__main__":
    asyncio.run(check())
