import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.analytics import normalize_metrics
from app.config import get_settings
from app.database import SessionLocal, engine
from app.models import (
    AccountAnalyticsSnapshot,
    ComplimentaryGrant,
    MediaAsset,
    Post,
    Publication,
    PublicationMetricSnapshot,
    User,
)

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


def creator(client, monkeypatch, plan="pro"):
    monkeypatch.setattr(get_settings(), "billing_enforcement_enabled", True)
    monkeypatch.setattr("app.analytics.utcnow", lambda: NOW)
    email = f"performance-{uuid4().hex}@example.com"
    response = client.post("/v1/auth/register", json={"name": "Creator", "email": email, "password": "creator123"})
    assert response.status_code == 201, response.text
    auth = response.json()

    async def grant():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == email))
            user.email_verified = True
            actual_now = datetime.now(UTC)
            if plan:
                db.add(ComplimentaryGrant(user_id=user.id, plan=plan,
                                          starts_at=actual_now - timedelta(days=1), ends_at=actual_now + timedelta(days=30),
                                          reason="Analytics test access"))
            await db.commit()
    asyncio.run(grant())
    return auth["user"]["id"], {"Authorization": f"Bearer {auth['access_token']}"}


async def seed(user_id, count=1, platform="instagram", age=1, status="published"):
    ids = []
    async with SessionLocal() as db:
        media = MediaAsset(user_id=user_id, original_name="video.mp4", mime_type="video/mp4",
                           size_bytes=100, uploaded_bytes=100, storage_path="test-only.mp4", status="ready", duration_seconds=30)
        db.add(media)
        await db.flush()
        for index in range(count):
            post = Post(user_id=user_id, media_id=media.id, title="Shared title", caption="Shared caption", status="partially_published")
            db.add(post)
            await db.flush()
            pub = Publication(post_id=post.id, platform=platform, status=status, published_at=NOW - timedelta(days=age),
                              submitted_title="", submitted_caption=f"Platform caption {index}")
            db.add(pub)
            await db.flush()
            ids.append((post.id, pub.id))
        await db.commit()
    return ids


@pytest.mark.parametrize("plan", [None, "basic"])
def test_performance_requires_pro(client, monkeypatch, plan):
    _, headers = creator(client, monkeypatch, plan)
    response = client.get("/v1/analytics/posts", headers=headers)
    assert response.status_code == 403
    assert response.json()["code"] == "upgrade_required"


def test_platform_time_filter_and_ownership(client, monkeypatch):
    owner, headers = creator(client, monkeypatch)
    other, _ = creator(client, monkeypatch)
    post_id, _ = asyncio.run(seed(owner, platform="tiktok"))[0]
    asyncio.run(seed(other, platform="tiktok"))
    asyncio.run(seed(owner, platform="tiktok", age=31))
    asyncio.run(seed(owner, platform="tiktok", status="failed"))

    async def earlier_platform():
        async with SessionLocal() as db:
            db.add(Publication(post_id=post_id, platform="instagram", status="published", published_at=NOW - timedelta(days=60)))
            await db.commit()
    asyncio.run(earlier_platform())
    response = client.get("/v1/analytics/posts?platform=tiktok&period=30d", headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["post_id"] for item in items] == [post_id]
    assert items[0]["caption"] == "Platform caption 0"
    assert items[0]["title"] == ""  # Intentionally empty submitted title stays empty.
    assert items[0]["analytics"]["metrics"] is None
    assert items[0]["analytics"]["availability"] == "pending"
    assert not client.get("/v1/analytics/posts?platform=instagram&period=30d", headers=headers).json()["items"]
    assert len(client.get("/v1/analytics/posts?platform=instagram&period=90d", headers=headers).json()["items"]) == 1


def test_pagination_has_no_eight_or_hundred_post_ceiling_and_stable_ties(client, monkeypatch):
    owner, headers = creator(client, monkeypatch)
    expected = {post_id for post_id, _ in asyncio.run(seed(owner, count=105))}
    seen = []
    cursor = None
    first_cursor = None
    while True:
        params = {"platform": "instagram", "period": "30d", "limit": 20}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/v1/analytics/posts", headers=headers, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        seen.extend(item["post_id"] for item in body["items"])
        cursor = body["next_cursor"]
        first_cursor = first_cursor or cursor
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 105
    assert set(seen) == expected
    mismatch = client.get("/v1/analytics/posts", headers=headers, params={"platform": "tiktok", "cursor": first_cursor})
    assert mismatch.status_code == 422
    other, other_headers = creator(client, monkeypatch)
    assert other != owner
    assert client.get("/v1/analytics/posts", headers=other_headers, params={"cursor": first_cursor}).status_code == 422


@pytest.mark.parametrize("params", [{"cursor": "not-json"}, {"cursor": "W10"}, {"platform": "bad"}, {"period": "365d"}, {"limit": 0}, {"limit": 51}])
def test_invalid_queries_are_rejected(client, monkeypatch, params):
    _, headers = creator(client, monkeypatch)
    assert client.get("/v1/analytics/posts", headers=headers, params=params).status_code == 422


def test_failed_refresh_preserves_successful_metrics_and_corrects_old_labels(client, monkeypatch):
    owner, headers = creator(client, monkeypatch)
    post_id, publication_id = asyncio.run(seed(owner))[0]

    async def snapshots():
        async with SessionLocal() as db:
            db.add(PublicationMetricSnapshot(publication_id=publication_id, captured_at=NOW - timedelta(hours=2),
                                            raw_metrics={"post_metrics": {"views": 42, "likes": 0, "comments": 3}},
                                            normalized_metrics={"exposure": 42, "likes": 0, "comments": 3, "shares": None},
                                            primary_metric="views", primary_label="Accounts reached", provider_status="available"))
            db.add(PublicationMetricSnapshot(publication_id=publication_id, captured_at=NOW - timedelta(hours=1),
                                            provider_status="unavailable", error_message="Provider temporarily unavailable"))
            db.add(AccountAnalyticsSnapshot(user_id=owner, platform="instagram", captured_at=NOW - timedelta(minutes=5)))
            await db.commit()
    asyncio.run(snapshots())
    body = client.get("/v1/analytics/posts", headers=headers).json()
    analytics = body["items"][0]["analytics"]
    assert analytics["primary_label"] == "Views"
    assert analytics["metrics"]["likes"] == 0
    assert analytics["metrics"]["shares"] is None
    assert analytics["stale"] is True
    assert analytics["availability"] == "unavailable"
    assert "10:00:00" in analytics["captured_at"]
    assert "11:00:00" in analytics["last_attempt_at"]
    assert "12:10:00" in body["refresh_available_at"]
    detail = client.get(f"/v1/analytics/posts/{post_id}", headers=headers).json()
    assert detail["items"][0]["primary_label"] == "Views"
    assert client.post("/v1/analytics/refresh", headers=headers).status_code == 429


def test_list_reads_snapshots_in_bounded_queries_without_provider_calls(client, monkeypatch):
    owner, headers = creator(client, monkeypatch)
    asyncio.run(seed(owner, count=20))
    def no_provider(*args, **kwargs):
        raise AssertionError("Reading performance must not call the provider")
    monkeypatch.setattr("app.analytics.UploadPostClient", no_provider)
    statements = []
    def collect(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(engine.sync_engine, "before_cursor_execute", collect)
    try:
        assert client.get("/v1/analytics/posts", headers=headers).status_code == 200
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", collect)
    assert len(statements) <= 12  # Fixed auth/access + page/media + two latest-snapshot reads.


@pytest.mark.parametrize("platform,payload,label", [
    ("instagram", {"views": 12}, "Views"),
    ("instagram", {"reach": 12}, "Accounts reached"),
    ("facebook", {"impressions": 12}, "Impressions"),
    ("facebook", {"reach": 12}, "People reached"),
    ("tiktok", {"play_count": 12}, "Plays"),
])
def test_actual_metric_labels(platform, payload, label):
    metrics, _, actual_label = normalize_metrics(platform, payload)
    assert actual_label == label
    assert metrics["exposure"] == 12


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", "Infinity", True])
def test_nonfinite_metrics_are_missing_not_zero(value):
    metrics, primary, _ = normalize_metrics("youtube", {"views": value})
    assert metrics["exposure"] is None
    assert primary is None
