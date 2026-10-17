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


async def seed(user_id, count=1, platform="instagram", age=1, status="published", views=10):
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
            if views is not None:
                db.add(PublicationMetricSnapshot(publication_id=pub.id, captured_at=NOW - timedelta(hours=5),
                       raw_metrics={"post_metrics": {"views": views}}, provider_status="available"))
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
            earlier = Publication(post_id=post_id, platform="instagram", status="published", published_at=NOW - timedelta(days=60))
            db.add(earlier)
            await db.flush()
            db.add(PublicationMetricSnapshot(publication_id=earlier.id, captured_at=NOW,
                   raw_metrics={"post_metrics": {"views": 5}}, provider_status="available"))
            await db.commit()
    asyncio.run(earlier_platform())
    response = client.get("/v1/analytics/posts?platform=tiktok&period=30d", headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["post_id"] for item in items] == [post_id]
    assert items[0]["caption"] == "Platform caption 0"
    assert items[0]["title"] == ""  # Intentionally empty submitted title stays empty.
    assert items[0]["analytics"]["metrics"]["views"] == 10
    assert items[0]["analytics"]["availability"] == "available"
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
    assert body["items"][0]["analytics"]["metrics"]["views"] == 42
    assert body["items"][0]["analytics"]["stale"] is True
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 42
    # A temporary refresh failure preserves the same saved views in both screens.
    analytics = client.get(f"/v1/analytics/posts/{post_id}", headers=headers).json()["items"][0]
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


@pytest.mark.parametrize("payload,expected", [
    ({"post_metrics": {"views": 8500, "reach": 6200}, "primary_impressions_field": "reach"}, 8500),
    ({"post_metrics": {"reach": 6200, "impressions": 9100}}, None),
    ({"post_metrics": {"views": 0, "reach": 100}}, 0),
    ({"post_metrics": {"views": -3}}, None),
    ({"profile_snapshot_latest": {"views": 8500}}, None),
    ({"post_metrics": None, "views": 8500}, None),
])
def test_post_views_never_use_reach_impressions_or_profile_totals(payload, expected):
    from app.analytics import normalize_post_metrics
    metrics, _, _ = normalize_post_metrics("instagram", payload)
    assert metrics["views"] == expected


def test_summary_visibility_and_platform_isolation(client, monkeypatch):
    owner, headers = creator(client, monkeypatch)
    other, other_headers = creator(client, monkeypatch)
    post_id, pub_id = asyncio.run(seed(owner))[0]
    _, missing_id = asyncio.run(seed(owner))[0]
    _, zero_id = asyncio.run(seed(owner))[0]
    _, old_id = asyncio.run(seed(owner, age=31))[0]
    _, foreign_id = asyncio.run(seed(other))[0]

    async def snapshots():
        async with SessionLocal() as db:
            youtube = Publication(post_id=post_id, platform="youtube", status="published", published_at=NOW - timedelta(days=1))
            db.add(youtube)
            await db.flush()
            for publication_id, raw in [(pub_id, {"views": 85, "reach": 62}), (youtube.id, {"views": 15}),
                                        (missing_id, {"reach": 999}), (zero_id, {"views": 0}),
                                        (old_id, {"views": 1000}), (foreign_id, {"views": 10000})]:
                db.add(PublicationMetricSnapshot(publication_id=publication_id,
                       captured_at=NOW - timedelta(hours=2), raw_metrics={"post_metrics": raw},
                       provider_status="available"))
            # Failed refresh retains the last valid 85; missing metrics retain their prior count.
            db.add(PublicationMetricSnapshot(publication_id=pub_id, captured_at=NOW - timedelta(hours=3),
                   raw_metrics={"post_metrics": {"views": 50}}, provider_status="available"))
            db.add(PublicationMetricSnapshot(publication_id=pub_id, captured_at=NOW - timedelta(hours=1),
                   provider_status="unavailable", error_message="Token expired"))
            await db.commit()
    asyncio.run(snapshots())
    summary = client.get("/v1/analytics/summary", headers=headers).json()
    assert summary["total_views"] == 1110
    assert summary["publication_count"] == 5
    assert summary["reporting_count"] == 5
    assert summary["missing_count"] == 0
    assert summary["stale_count"] == 2
    assert summary["top_post"]["views"] == 1000
    assert client.get("/v1/analytics/summary?period=30d", headers=headers).json()["total_views"] == 110
    path = f"/v1/analytics/publications/{pub_id}/visibility"
    assert client.patch(path, headers=other_headers, json={"hidden": True}).status_code == 404
    assert client.patch(path, headers=headers, json={"hidden": True}).status_code == 200
    assert pub_id not in [item["publication_id"] for item in client.get("/v1/analytics/posts", headers=headers).json()["items"]]
    hidden = client.get("/v1/analytics/posts?hidden=true", headers=headers).json()["items"]
    assert len(hidden) == 1 and hidden[0]["publication_id"] == pub_id
    assert hidden[0]["hidden_reason"] == "user"
    assert hidden[0]["analytics"]["metrics"]["views"] == 85
    assert hidden[0]["analytics"]["failure_reason"] == "connection"
    assert len(client.get("/v1/analytics/posts?platform=youtube", headers=headers).json()["items"]) == 1
    hidden_summary = client.get("/v1/analytics/summary", headers=headers).json()
    assert hidden_summary["total_views"] == 1025 and hidden_summary["hidden_count"] == 0
    assert client.patch(path, headers=headers, json={"hidden": False}).status_code == 200
    assert not client.get("/v1/analytics/posts?hidden=true", headers=headers).json()["items"]
    detail = client.get(f"/v1/analytics/posts/{post_id}", headers=headers).json()["items"]
    instagram = next(item for item in detail if item["platform"] == "instagram")
    assert instagram["metrics"]["views"] == 85
    assert instagram["metrics"]["reach"] == 62
    assert [item["views"] for item in instagram["history"]] == [10, 50, 85]


def test_hidden_cursor_is_bound_to_visibility(client, monkeypatch):
    owner, headers = creator(client, monkeypatch)
    asyncio.run(seed(owner, count=2))
    page = client.get("/v1/analytics/posts?limit=1", headers=headers).json()
    response = client.get("/v1/analytics/posts", headers=headers, params={"cursor": page["next_cursor"], "hidden": True})
    assert response.status_code == 422


@pytest.mark.parametrize("error,hidden", [
    ("Video has been deleted.", True),
    ("Video not found. The token may need to be refreshed.", False),
    ("Unsupported get request", False),
    ("Post may have been deleted or you lack permission", False),
    ("Token expired", False),
    ("Provider temporarily unavailable", False),
])
def test_sync_only_hides_explicit_deletions(client, monkeypatch, error, hidden):
    from app.analytics import sync_user_analytics
    from app.providers import ProviderError
    owner, headers = creator(client, monkeypatch)
    _, pub_id = asyncio.run(seed(owner))[0]

    async def fail(*args, **kwargs):
        raise ProviderError("analytics_unavailable", error)

    async def periods(*args, **kwargs):
        pass
    monkeypatch.setattr("app.analytics.UploadPostClient.post_analytics", fail)
    monkeypatch.setattr("app.analytics._sync_period_snapshots", periods)

    async def sync():
        async with SessionLocal() as db:
            user = await db.get(User, owner)
            await sync_user_analytics(db, user, force=True)
    asyncio.run(sync())
    rows = client.get("/v1/analytics/posts", headers=headers, params={"hidden": hidden}).json()["items"]
    if hidden:
        assert len(rows) == 1 and rows[0]["publication_id"] == pub_id
        assert rows[0]["hidden_reason"] == "deleted"
    else:
        assert len(rows) == 1
        assert rows[0]["analytics"]["metrics"]["views"] == 10
    expected = None if hidden else 10
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == expected


def test_empty_summary_and_pro_access(client, monkeypatch):
    _, headers = creator(client, monkeypatch)
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] is None
    owner, basic = creator(client, monkeypatch, "basic")
    _, pub_id = asyncio.run(seed(owner))[0]
    assert client.get("/v1/analytics/summary", headers=basic).status_code == 403
    assert client.patch(f"/v1/analytics/publications/{pub_id}/visibility", headers=basic, json={"hidden": True}).status_code == 403


def test_unusable_posts_are_excluded_before_pagination_and_recover_on_refresh(client, monkeypatch):
    owner, headers = creator(client, monkeypatch)
    asyncio.run(seed(owner, count=24, views=None))
    good = asyncio.run(seed(owner, count=3, views=0, age=2))
    bad = asyncio.run(seed(owner, count=3, views=None))

    async def snapshots():
        async with SessionLocal() as db:
            for (_, publication_id), raw in zip(bad, [{"reach": 605}, {"views": -1}, {"views": "NaN"}], strict=True):
                db.add(PublicationMetricSnapshot(publication_id=publication_id, captured_at=NOW - timedelta(hours=1),
                       raw_metrics={"post_metrics": raw}, provider_status="available"))
            await db.commit()
    asyncio.run(snapshots())
    first = client.get("/v1/analytics/posts?limit=2", headers=headers).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = client.get("/v1/analytics/posts", headers=headers, params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert {item["publication_id"] for item in first["items"] + second["items"]} == {pub for _, pub in good}
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 0

    async def recover():
        async with SessionLocal() as db:
            db.add(PublicationMetricSnapshot(publication_id=bad[0][1], captured_at=NOW,
                   raw_metrics={"post_metrics": {"views": 80}}, provider_status="available"))
            await db.commit()
    asyncio.run(recover())
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 80
    path = f"/v1/analytics/publications/{bad[0][1]}/visibility"
    assert client.patch(path, headers=headers, json={"hidden": True}).status_code == 200
    result = client.get("/v1/analytics/summary", headers=headers).json()
    assert result["total_views"] == 0 and result["publication_count"] == 3
    assert result["top_post"]["views"] == 0
    assert result["platforms"] == [{"platform": "instagram", "views": 0}]
    assert client.patch(path, headers=headers, json={"hidden": False}).status_code == 200
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 80


def test_rate_limited_refresh_resumes_after_completed_posts(client, monkeypatch):
    from app.analytics_runtime import AnalyticsDeferred
    from app.tasks import _sync_analytics

    owner, headers = creator(client, monkeypatch)
    asyncio.run(seed(owner, count=3, views=10))
    seen = []
    async def periods(*args, **kwargs):
        pass
    async def read(self, **kwargs):
        # Seed IDs are absent, but each call represents a different publication.
        seen.append(kwargs)
        if len(seen) == 2:
            raise AnalyticsDeferred(60)
        return {"instagram": {"post_metrics": {"views": 80}}}
    monkeypatch.setattr("app.analytics._sync_period_snapshots", periods)
    monkeypatch.setattr("app.analytics.UploadPostClient.post_analytics", read)
    monkeypatch.setattr("app.tasks.generate_analytics_insights.delay", lambda *_args: None)
    with pytest.raises(AnalyticsDeferred):
        asyncio.run(_sync_analytics(owner, True, NOW.timestamp()))
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 100
    asyncio.run(_sync_analytics(owner, True, NOW.timestamp()))
    assert len(seen) == 4  # 3 successful reads plus one rejected call, not 5.
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 240


@pytest.mark.parametrize("error,status,expected", [
    ("Too many requests", 429, 0), ("Upstream server error", 503, 0),
    ("Token expired", 401, 0), ("Video has been deleted", 404, None),
])
def test_unconfirmed_failures_keep_last_actual_zero_views(client, monkeypatch, error, status, expected):
    owner, headers = creator(client, monkeypatch)
    _, pub_id = asyncio.run(seed(owner, views=0))[0]
    asyncio.run(seed(owner, views=None))
    async def failure():
        async with SessionLocal() as db:
            db.add(PublicationMetricSnapshot(publication_id=pub_id, captured_at=NOW - timedelta(minutes=1),
                   raw_metrics={"_refresh_failure": {"status": status}}, provider_status="unavailable", error_message=error))
            await db.commit()
    asyncio.run(failure())
    result = client.get("/v1/analytics/summary", headers=headers).json()
    rows = client.get("/v1/analytics/posts", headers=headers).json()["items"]
    assert result["total_views"] == expected
    assert len(rows) == (1 if expected is not None else 0)


@pytest.mark.parametrize("platform,raw,views,impressions,label", [
    ("x", {"impressions": 29, "likes": 2, "replies": 4, "reposts": 1}, None, 29, "Impressions"),
    ("x", {"impressions": 0, "views": 8}, 8, 0, "Impressions"),
    ("x", {"impressions": -1}, None, None, None),
    ("x", {"impressions": True}, None, None, None),
    ("linkedin", {"video_views": 3, "impressions": 8}, 3, 8, "Views"),
    ("linkedin", {"impressions": 8}, None, 8, None),
])
def test_x_and_linkedin_keep_native_metric_meanings(platform, raw, views, impressions, label):
    from app.analytics import normalize_post_metrics
    metrics, _, actual_label = normalize_post_metrics(platform, {"post_metrics": raw})
    assert metrics["views"] == views
    assert metrics["impressions"] == impressions
    assert actual_label == label
    if raw.get("replies"):
        assert metrics["comments"] == 4
        assert metrics["shares"] == 1


def test_sync_new_platforms_totals_visibility_and_failures(client, monkeypatch):
    from app.analytics import sync_user_analytics
    from app.providers import ProviderError
    owner, headers = creator(client, monkeypatch)
    asyncio.run(seed(owner, platform="instagram"))
    x_post, x_pub = asyncio.run(seed(owner, platform="x", views=None))[0]
    asyncio.run(seed(owner, platform="linkedin", views=None))
    calls = []
    failure = None

    async def metrics(self, **kwargs):
        platform = kwargs["platform"]
        calls.append(platform)
        if platform == "x" and failure:
            raise ProviderError("provider_unavailable", *failure)
        raw = {"x": {"impressions": 29, "likes": 2, "replies": 4, "reposts": 1},
               "linkedin": {"video_views": 3, "impressions": 8},
               "instagram": {"views": 10, "impressions": 100}}[platform]
        return {"platforms": {platform: {"post_metrics": raw}}}

    async def periods(*args, **kwargs):
        pass

    async def sync():
        async with SessionLocal() as db:
            await sync_user_analytics(db, await db.get(User, owner), force=True)

    monkeypatch.setattr("app.analytics.UploadPostClient.post_analytics", metrics)
    monkeypatch.setattr("app.analytics._sync_period_snapshots", periods)
    asyncio.run(sync())
    assert set(calls) == {"instagram", "x", "linkedin"}
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 42
    row = client.get("/v1/analytics/posts?platform=x", headers=headers).json()["items"][0]
    assert row["analytics"]["metrics"]["views"] is None
    assert row["analytics"]["metrics"]["impressions"] == 29
    assert row["analytics"]["primary_label"] == "Impressions"
    li = client.get("/v1/analytics/posts?platform=linkedin", headers=headers).json()["items"][0]
    assert li["analytics"]["metrics"]["views"] == 3
    detail = client.get(f"/v1/analytics/posts/{x_post}", headers=headers).json()["items"][0]
    assert detail["primary_label"] == "Impressions"
    assert detail["metrics"]["comments"] == 4
    assert detail["metrics"]["shares"] == 1
    assert client.patch(f"/v1/analytics/publications/{x_pub}/visibility", headers=headers, json={"hidden": True}).status_code == 200
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 13
    assert client.get("/v1/analytics/posts?platform=x", headers=headers).json()["items"] == []
    client.patch(f"/v1/analytics/publications/{x_pub}/visibility", headers=headers, json={"hidden": False})
    failure = ("Too many requests", 429)
    asyncio.run(sync())
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 42
    failure = ("Token expired", 401)
    asyncio.run(sync())
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 42


@pytest.mark.parametrize("platform,metric", [("instagram", "views"), ("x", "impressions"), ("linkedin", "video_views")])
def test_missing_success_retains_last_valid_and_accepts_lower_corrections(client, monkeypatch, platform, metric):
    owner, headers = creator(client, monkeypatch)
    _, pub = asyncio.run(seed(owner, platform=platform, views=None))[0]

    async def reading(hours, raw, status="available", error=None):
        async with SessionLocal() as db:
            db.add(PublicationMetricSnapshot(publication_id=pub, captured_at=NOW - timedelta(hours=hours),
                   raw_metrics={"post_metrics": raw}, provider_status=status, error_message=error))
            await db.commit()
    for hours, raw in [(5, {metric: 100}), (4, {}), (3, {metric: -1}), (2, {metric: "NaN"})]:
        asyncio.run(reading(hours, raw))
    asyncio.run(reading(1, {}, "unavailable", "Unauthorized"))
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 100
    row = client.get(f"/v1/analytics/posts?platform={platform}", headers=headers).json()["items"][0]
    assert row["analytics"]["stale"] is True
    assert "07:00:00" in row["analytics"]["captured_at"]
    for hours, value in [(0.5, 80), (0, 0)]:
        asyncio.run(reading(hours, {metric: value}))
        assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == value
        row = client.get(f"/v1/analytics/posts?platform={platform}", headers=headers).json()["items"][0]
        assert row["analytics"]["stale"] is False


def test_all_time_does_not_age_out_but_insights_keeps_its_window(client, monkeypatch):
    owner, headers = creator(client, monkeypatch)
    asyncio.run(seed(owner, age=29, views=10))
    asyncio.run(seed(owner, age=120, views=20))
    asyncio.run(seed(owner, age=600, views=30))
    asyncio.run(seed(owner, age=-1, views=500))  # Future publications never count.
    response = client.get("/v1/analytics/summary?period=all", headers=headers).json()
    assert response["total_views"] == 60 and response["published_from"] is None
    assert response["period"] == "all"
    assert len(client.get("/v1/analytics/posts?period=30d", headers=headers).json()["items"]) == 1
    monkeypatch.setattr("app.analytics.utcnow", lambda: NOW + timedelta(days=2))
    # The former recent post remains in Home after leaving Insights.
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 560
    assert client.get("/v1/analytics/posts?period=30d", headers=headers).json()["items"][0]["analytics"]["metrics"]["views"] == 500


def test_old_publications_refresh_without_daily_refetch(client, monkeypatch):
    from app.analytics import sync_user_analytics
    owner, _ = creator(client, monkeypatch)
    _, pub = asyncio.run(seed(owner, age=600, views=None))[0]
    calls = []
    async def metrics(self, **kwargs):
        calls.append(kwargs)
        return {"instagram": {"post_metrics": {"views": 12}}}
    async def periods(*args, **kwargs):
        pass
    monkeypatch.setattr("app.analytics.UploadPostClient.post_analytics", metrics)
    monkeypatch.setattr("app.analytics._sync_period_snapshots", periods)
    async def sync():
        async with SessionLocal() as db:
            await sync_user_analytics(db, await db.get(User, owner))
    asyncio.run(sync())
    asyncio.run(sync())
    assert len(calls) == 1


def test_history_cleanup_keeps_latest_status_and_last_valid_count(client, monkeypatch):
    from app.tasks import _sync_due_analytics
    owner, headers = creator(client, monkeypatch)
    _, pub = asyncio.run(seed(owner, age=600, views=None))[0]
    async def readings():
        async with SessionLocal() as db:
            for age, raw, status in [(550, {"views": 100}, "available"),
                                     (520, {"views": 80}, "available"),
                                     (510, {}, "available"), (500, {}, "unavailable")]:
                db.add(PublicationMetricSnapshot(publication_id=pub, captured_at=NOW - timedelta(days=age),
                       raw_metrics={"post_metrics": raw}, provider_status=status, error_message="Timeout" if status == "unavailable" else None))
            await db.commit()
    asyncio.run(readings())
    monkeypatch.setattr("app.tasks.utcnow", lambda: NOW)
    monkeypatch.setattr("app.tasks.sync_analytics.delay", lambda *args: None)
    asyncio.run(_sync_due_analytics())
    async def remaining():
        async with SessionLocal() as db:
            return list(await db.scalars(select(PublicationMetricSnapshot).where(PublicationMetricSnapshot.publication_id == pub)))
    assert len(asyncio.run(remaining())) == 2
    assert client.get("/v1/analytics/summary", headers=headers).json()["total_views"] == 80
