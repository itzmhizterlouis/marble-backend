import asyncio

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import EntitlementAuditEvent, MediaAsset, Post, SocialConnection, User
from app.providers import ProviderError

ADMIN = "profile-admin@example.com"


def register(client, email: str) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/v1/auth/register",
        json={"name": "Profile Creator", "email": email, "password": "creator123"},
    )
    assert response.status_code == 201, response.text
    auth = response.json()
    return auth, {"Authorization": f"Bearer {auth['access_token']}"}


async def connect(email: str) -> str:
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        db.add(
            SocialConnection(
                user_id=user.id,
                platform="tiktok",
                status="connected",
                provider_account_id="tiktok:creator",
                username="creator",
            )
        )
        await db.commit()
        return user.upload_post_profile


async def connection_state(user_id: str) -> tuple[str, str | None, list[str]]:
    async with SessionLocal() as db:
        row = await db.scalar(select(SocialConnection).where(SocialConnection.user_id == user_id))
        actions = list(
            await db.scalars(select(EntitlementAuditEvent.action).where(EntitlementAuditEvent.user_id == user_id))
        )
        return row.status, row.provider_account_id, actions


def setup(client, monkeypatch, email: str):
    admin = f"admin-{email}"
    monkeypatch.setattr(get_settings(), "admin_emails", admin)
    _, admin_headers = register(client, admin)
    creator, _ = register(client, email)
    profile = asyncio.run(connect(email))
    return admin_headers, creator["user"]["id"], profile


def test_admin_deletes_profile_and_clears_connections(client, monkeypatch):
    admin_headers, user_id, profile = setup(client, monkeypatch, "delete-profile@example.com")
    deleted: list[str] = []

    async def fake_delete(self, username: str) -> dict:
        deleted.append(username)
        return {"success": True}

    monkeypatch.setattr("app.providers.UploadPostClient.delete_profile", fake_delete)
    response = client.post(
        f"/v1/admin/users/{user_id}/upload-post-profile/delete",
        headers=admin_headers,
        json={"reason": "Removing a test account"},
    )

    assert response.status_code == 200, response.text
    assert deleted == [profile]
    assert asyncio.run(connection_state(user_id)) == ("disconnected", None, ["upload_post_profile_deleted"])


def test_missing_profile_still_clears_connections(client, monkeypatch):
    admin_headers, user_id, _ = setup(client, monkeypatch, "gone-profile@example.com")

    async def not_found(self, username: str) -> dict:
        raise ProviderError("provider_http_404", "Profile not found", 404)

    monkeypatch.setattr("app.providers.UploadPostClient.delete_profile", not_found)
    response = client.post(
        f"/v1/admin/users/{user_id}/upload-post-profile/delete",
        headers=admin_headers,
        json={"reason": "Removing a test account"},
    )

    assert response.status_code == 200, response.text
    assert "already gone" in response.json()["message"]
    assert asyncio.run(connection_state(user_id))[0] == "disconnected"


def test_provider_failure_keeps_connections(client, monkeypatch):
    admin_headers, user_id, _ = setup(client, monkeypatch, "provider-down@example.com")

    async def unavailable(self, username: str) -> dict:
        raise ProviderError("provider_unavailable", "Upload-Post is temporarily unavailable", 503)

    monkeypatch.setattr("app.providers.UploadPostClient.delete_profile", unavailable)
    response = client.post(
        f"/v1/admin/users/{user_id}/upload-post-profile/delete",
        headers=admin_headers,
        json={"reason": "Removing a test account"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "profile_delete_failed"
    assert asyncio.run(connection_state(user_id)) == ("connected", "tiktok:creator", [])


def test_refuses_while_posts_are_scheduled_or_posting(client, monkeypatch):
    admin_headers, user_id, _ = setup(client, monkeypatch, "busy-profile@example.com")
    called: list[str] = []

    async def fake_delete(self, username: str) -> dict:
        called.append(username)
        return {"success": True}

    monkeypatch.setattr("app.providers.UploadPostClient.delete_profile", fake_delete)

    async def schedule_post() -> None:
        async with SessionLocal() as db:
            media = MediaAsset(
                user_id=user_id,
                original_name="later.mp4",
                mime_type="video/mp4",
                size_bytes=8,
                storage_path="uploads/later.mp4",
            )
            db.add(media)
            await db.flush()
            db.add(Post(user_id=user_id, media_id=media.id, caption="Later", status="scheduled"))
            await db.commit()

    asyncio.run(schedule_post())
    response = client.post(
        f"/v1/admin/users/{user_id}/upload-post-profile/delete",
        headers=admin_headers,
        json={"reason": "Removing a test account"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "profile_in_use"
    assert called == []


def test_non_admin_cannot_delete_profiles(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_emails", ADMIN)
    creator, headers = register(client, "not-admin@example.com")
    response = client.post(
        f"/v1/admin/users/{creator['user']['id']}/upload-post-profile/delete",
        headers=headers,
        json={"reason": "Trying without access"},
    )
    assert response.status_code == 403


def test_admin_list_shows_connected_platforms(client, monkeypatch):
    admin_headers, user_id, _ = setup(client, monkeypatch, "count-connections@example.com")

    async def add_more() -> None:
        async with SessionLocal() as db:
            db.add(SocialConnection(user_id=user_id, platform="facebook", status="needs_selection"))
            db.add(SocialConnection(user_id=user_id, platform="youtube", status="expired"))
            db.add(SocialConnection(user_id=user_id, platform="x", status="disconnected"))
            await db.commit()

    asyncio.run(add_more())
    response = client.get("/v1/admin/users?q=count-connections", headers=admin_headers)

    assert response.status_code == 200, response.text
    creator = next(item for item in response.json()["items"] if item["id"] == user_id)
    assert creator["connected_platforms"] == ["facebook", "tiktok"]


def test_admin_list_reports_platform_totals(client, monkeypatch):
    admin_headers, user_id, _ = setup(client, monkeypatch, "totals@example.com")

    async def verify() -> None:
        async with SessionLocal() as db:
            user = await db.get(User, user_id)
            user.email_verified = True
            await db.commit()

    asyncio.run(verify())
    body = client.get("/v1/admin/users?q=totals@example.com", headers=admin_headers).json()

    assert any(item["id"] == user_id for item in body["items"])
    assert body["total_users"] >= 2
    assert 1 <= body["verified_users"] <= body["total_users"]


def test_admin_list_filters_to_creators_with_connected_accounts(client, monkeypatch):
    admin_headers, user_id, _ = setup(client, monkeypatch, "conn-filter-on@example.com")
    other, _ = register(client, "conn-filter-expired@example.com")

    async def expire_only() -> None:
        async with SessionLocal() as db:
            db.add(SocialConnection(user_id=other["user"]["id"], platform="youtube", status="expired"))
            await db.commit()

    asyncio.run(expire_only())
    body = client.get("/v1/admin/users?q=conn-filter-&has_connections=true", headers=admin_headers).json()

    assert [item["id"] for item in body["items"]] == [user_id]
    assert body["matching_users"] == 1
