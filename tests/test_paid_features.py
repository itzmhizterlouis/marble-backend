import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.analytics import normalize_metrics
from app.config import get_settings
from app.database import SessionLocal
from app.models import ComplimentaryGrant, MediaAsset, Post, Publication, SocialConnection, Subscription, User


def register(client, email: str) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/v1/auth/register",
        json={"name": "Paid Creator", "email": email, "password": "creator123"},
    )
    assert response.status_code == 201
    auth = response.json()
    return auth, {"Authorization": f"Bearer {auth['access_token']}"}


async def verify_user(email: str, *, grant: str | None = None) -> None:
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        user.email_verified = True
        db.add(
            SocialConnection(
                user_id=user.id,
                platform="instagram",
                status="connected",
                provider_account_id=f"instagram:{user.id}",
            )
        )
        if grant:
            now = datetime.now(UTC)
            db.add(
                ComplimentaryGrant(
                    user_id=user.id,
                    plan=grant,
                    starts_at=now - timedelta(minutes=1),
                    ends_at=now + timedelta(days=30),
                    reason="Automated test access",
                )
            )
        await db.commit()


async def mark_ready(media_ids: list[str]) -> None:
    async with SessionLocal() as db:
        for media_id in media_ids:
            media = await db.get(MediaAsset, media_id)
            media.status = "ready"
            media.duration_seconds = 10
            media.width = 1080
            media.height = 1920
        await db.commit()


def create_draft(client, headers: dict[str, str], suffix: str) -> dict:
    media = client.post(
        "/v1/media",
        headers=headers,
        json={"filename": f"{suffix}.mp4", "mime_type": "video/mp4", "size_bytes": 8},
    )
    assert media.status_code == 201, media.text
    asyncio.run(mark_ready([media.json()["id"]]))
    post = client.post(
        "/v1/posts",
        headers=headers,
        json={
            "media_id": media.json()["id"],
            "caption": "A real creator post",
            "versions": [{"platform": "instagram", "caption": "A real creator post"}],
        },
    )
    assert post.status_code == 201, post.text
    return post.json()


def test_one_post_preview_is_atomic_and_allows_same_post_retry(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    email = "one-post-preview@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email))

    # Prepare both drafts before the allowance is consumed to simulate two tabs.
    first = create_draft(client, headers, "first")
    second = create_draft(client, headers, "second")
    monkeypatch.setattr("app.posts.publish_post.delay", lambda _post_id: None)
    monkeypatch.setattr("app.posts.retry_platform.delay", lambda _post_id, _platform: None)

    schedule = client.post(
        f"/v1/posts/{first['id']}/publish",
        headers=headers,
        json={
            "mode": "scheduled",
            "scheduled_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "timezone": "Africa/Lagos",
        },
    )
    assert schedule.status_code == 403
    assert schedule.json()["code"] == "upgrade_required"

    published = client.post(f"/v1/posts/{first['id']}/publish", headers=headers, json={"mode": "now"})
    assert published.status_code == 202, published.text
    denied = client.post(f"/v1/posts/{second['id']}/publish", headers=headers, json={"mode": "now"})
    assert denied.status_code == 403
    assert denied.json()["code"] == "upgrade_required"

    async def fail_first() -> None:
        async with SessionLocal() as db:
            publication = await db.scalar(select(Publication).where(Publication.post_id == first["id"]))
            publication.status = "failed"
            await db.commit()

    asyncio.run(fail_first())
    retry = client.post(
        f"/v1/posts/{first['id']}/publications/instagram/retry",
        headers=headers,
    )
    assert retry.status_code == 202, retry.text


def test_deleting_trial_post_does_not_restore_preview(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    email = "deleted-preview@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email))
    first = create_draft(client, headers, "delete-first")
    second = create_draft(client, headers, "delete-second")
    monkeypatch.setattr("app.posts.publish_post.delay", lambda _post_id: None)
    assert client.post(f"/v1/posts/{first['id']}/publish", headers=headers, json={"mode": "now"}).status_code == 202

    async def finish_first() -> None:
        async with SessionLocal() as db:
            post = await db.get(Post, first["id"])
            publication = await db.scalar(select(Publication).where(Publication.post_id == first["id"]))
            post.status = "published"
            publication.status = "published"
            await db.commit()

    asyncio.run(finish_first())
    assert client.delete(f"/v1/posts/{first['id']}", headers=headers).status_code == 200
    denied = client.post(f"/v1/posts/{second['id']}/publish", headers=headers, json={"mode": "now"})
    assert denied.status_code == 403
    assert denied.json()["code"] == "upgrade_required"


def test_paid_pro_takes_precedence_over_launch_basic_grant(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    email = "upgrade-from-grant@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email, grant="basic"))

    async def add_pro_subscription() -> None:
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == email))
            db.add(
                Subscription(
                    user_id=user.id,
                    plan="pro",
                    status="active",
                    reference="paid-pro-reference",
                    amount_kobo=2_000_000,
                    currency="NGN",
                    paid_through=datetime.now(UTC) + timedelta(days=31),
                )
            )
            await db.commit()

    asyncio.run(add_pro_subscription())
    access = client.get("/v1/billing/me", headers=headers)
    assert access.status_code == 200
    assert access.json()["tier"] == "pro"
    assert access.json()["capabilities"]["analytics"] is True


def test_pro_access_queues_one_ai_video_job_at_a_time(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")
    email = "pro-ai@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email, grant="pro"))
    post = create_draft(client, headers, "ai-source")
    monkeypatch.setattr("app.tasks.run_ai_generation.delay", lambda _job_id: None)

    context = "This is for Nigerian creators. Keep the tone warm and end with a question."
    queued = client.post(
        "/v1/ai/generations",
        headers=headers,
        json={"post_id": post["id"], "generation_context": f"  {context}  "},
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["status"] == "queued"
    assert queued.json()["generation_context"] == context
    latest = client.get(f"/v1/ai/generations/latest?post_id={post['id']}", headers=headers)
    assert latest.status_code == 200, latest.text
    assert latest.json()["id"] == queued.json()["id"]
    assert latest.json()["generation_context"] == context
    duplicate = client.post("/v1/ai/generations", headers=headers, json={"post_id": post["id"]})
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "ai_generation_in_progress"

    too_long = client.post(
        "/v1/ai/generations",
        headers=headers,
        json={"post_id": post["id"], "generation_context": "x" * 2001},
    )
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "validation_error"


def test_paystack_webhook_signature_and_deduplication(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "paystack_secret_key", "paystack-test-secret")
    monkeypatch.setattr("app.tasks.process_billing_event.delay", lambda _event_id: None)
    body = json.dumps({"event": "subscription.disable", "data": {"subscription_code": "SUB_test"}}).encode()
    signature = hmac.new(settings.paystack_secret_key.encode(), body, hashlib.sha512).hexdigest()
    headers = {"x-paystack-signature": signature, "Content-Type": "application/json"}
    first = client.post("/v1/webhooks/paystack", content=body, headers=headers)
    second = client.post("/v1/webhooks/paystack", content=body, headers=headers)
    assert first.status_code == second.status_code == 200
    invalid = client.post("/v1/webhooks/paystack", content=body, headers={"x-paystack-signature": "bad"})
    assert invalid.status_code == 401


def test_platform_metrics_preserve_missing_values():
    normalized, metric, label = normalize_metrics(
        "instagram", {"reach": 1250, "likes": 42, "comments": 7}
    )
    assert normalized == {"exposure": 1250, "likes": 42, "comments": 7, "shares": None}
    assert metric == "reach"
    assert label == "Accounts reached"

    missing, metric, label = normalize_metrics("youtube", {"likes": 2})
    assert missing["exposure"] is None
    assert metric is None
    assert label is None
