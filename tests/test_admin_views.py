import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.config import get_settings
from app.database import SessionLocal
from app.models import MediaAsset, Post, Publication, PublicationMetricSnapshot


def register(client, email: str) -> tuple[str, dict[str, str]]:
    response = client.post("/v1/auth/register", json={"name": "Creator", "email": email, "password": "creator123"})
    assert response.status_code == 201, response.text
    auth = response.json()
    return auth["user"]["id"], {"Authorization": f"Bearer {auth['access_token']}"}


async def publish(user_id: str, platform: str, views: int | None, *, hidden: bool = False, status: str = "published",
                  raw: dict | None = None, error: str | None = None) -> None:
    async with SessionLocal() as db:
        media = MediaAsset(user_id=user_id, original_name="v.mp4", mime_type="video/mp4", size_bytes=1, uploaded_bytes=1,
                           storage_path="test-only.mp4", status="ready", duration_seconds=30)
        db.add(media)
        await db.flush()
        post = Post(user_id=user_id, media_id=media.id, title="T", caption="C", status="published")
        db.add(post)
        await db.flush()
        pub = Publication(post_id=post.id, platform=platform, status=status, published_at=datetime.now(UTC) - timedelta(days=1),
                          submitted_title="", submitted_caption="c",
                          analytics_hidden_at=datetime.now(UTC) if hidden else None)
        db.add(pub)
        await db.flush()
        if raw is not None:
            db.add(PublicationMetricSnapshot(publication_id=pub.id, captured_at=datetime.now(UTC) - timedelta(hours=1),
                                             raw_metrics={"post_metrics": raw}, provider_status="available"))
        if error is not None:
            db.add(PublicationMetricSnapshot(publication_id=pub.id, captured_at=datetime.now(UTC),
                                             raw_metrics={}, provider_status="unavailable", error_message=error))
        if views is not None:
            key = "impressions" if platform == "x" else "views"
            db.add(PublicationMetricSnapshot(publication_id=pub.id, captured_at=datetime.now(UTC) - timedelta(hours=1),
                                             raw_metrics={"post_metrics": {key: views}}, provider_status="available"))
        await db.commit()


def test_admin_views_summary_totals_visible_published_posts_only(client, monkeypatch):
    admin_email = f"views-admin-{uuid4().hex}@example.com"
    monkeypatch.setattr(get_settings(), "admin_emails", admin_email)
    _, admin_headers = register(client, admin_email)
    before = client.get("/v1/admin/views-summary", headers=admin_headers).json()
    owner, _ = register(client, f"views-owner-{uuid4().hex}@example.com")

    asyncio.run(publish(owner, "tiktok", 100))
    asyncio.run(publish(owner, "youtube", 50))
    asyncio.run(publish(owner, "x", 7))
    asyncio.run(publish(owner, "instagram", 999, hidden=True))   # hidden by the creator
    asyncio.run(publish(owner, "facebook", None))                # no reading yet
    asyncio.run(publish(owner, "linkedin", 500, status="failed"))  # never published

    after = client.get("/v1/admin/views-summary", headers=admin_headers).json()

    assert after["total_views"] - before["total_views"] == 157
    assert after["counted_posts"] - before["counted_posts"] == 3
    assert after["creators_with_posts"] - before["creators_with_posts"] == 1
    by_platform = {item["platform"]: item["views"] for item in after["platforms"]}
    assert by_platform["x"] >= 7 and by_platform["tiktok"] >= 100


def test_views_summary_is_admin_only(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_emails", "someone-else@example.com")
    _, headers = register(client, f"views-user-{uuid4().hex}@example.com")
    assert client.get("/v1/admin/views-summary", headers=headers).status_code == 403


def diag(client, headers) -> dict:
    body = client.get("/v1/admin/analytics-diagnostics", headers=headers).json()
    return {row["platform"]: row for row in body["platforms"]}


def test_analytics_diagnostics_explain_missing_views(client, monkeypatch):
    admin_email = f"diag-admin-{uuid4().hex}@example.com"
    monkeypatch.setattr(get_settings(), "admin_emails", admin_email)
    _, admin_headers = register(client, admin_email)
    before = diag(client, admin_headers)
    owner, _ = register(client, f"diag-owner-{uuid4().hex}@example.com")

    asyncio.run(publish(owner, "linkedin", None, raw={"impressions": 40, "likes": 2}))   # reach/impressions only
    asyncio.run(publish(owner, "linkedin", 5))                                              # real views
    asyncio.run(publish(owner, "facebook", None, error="Permissions error: reconnect your Page"))
    asyncio.run(publish(owner, "facebook", None))                                           # never refreshed
    asyncio.run(publish(owner, "x", None, raw={"impressions": 12}))

    after = diag(client, admin_headers)

    def delta(platform: str, key: str) -> int:
        return after[platform][key] - before.get(platform, {}).get(key, 0)

    assert delta("linkedin", "published_posts") == 2
    assert delta("linkedin", "with_reading") == 1
    assert delta("linkedin", "reading_without_views") == 1
    assert delta("facebook", "failed_refreshes") == 1
    assert delta("facebook", "no_reading_yet") == 1
    assert delta("x", "with_reading") == 1
    assert {"name": "impressions", "posts": 1} in [
        item for item in after["linkedin"]["metric_names"] if item["name"] == "impressions" and item["posts"] >= 1
    ] or any(item["name"] == "impressions" for item in after["linkedin"]["metric_names"])
    assert any("reconnect your Page" in item["message"] for item in after["facebook"]["errors"])


def test_analytics_diagnostics_are_admin_only(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_emails", "someone-else@example.com")
    _, headers = register(client, f"diag-user-{uuid4().hex}@example.com")
    assert client.get("/v1/admin/analytics-diagnostics", headers=headers).status_code == 403
