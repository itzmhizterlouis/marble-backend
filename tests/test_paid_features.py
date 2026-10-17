import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.analytics import _merge_ai_explanations, normalize_metrics
from app.auth import create_one_time_token
from app.config import get_settings
from app.database import SessionLocal
from app.entitlements import start_trial
from app.models import (
    AIGenerationJob,
    AnalyticsPeriodSnapshot,
    ComplimentaryGrant,
    MediaAsset,
    Publication,
    PublicationMetricSnapshot,
    SocialConnection,
    Subscription,
    User,
)
from app.tasks import _run_ai_generation


def register(client, email: str) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/v1/auth/register",
        json={"name": "Paid Creator", "email": email, "password": "creator123"},
    )
    assert response.status_code == 201
    auth = response.json()
    return auth, {"Authorization": f"Bearer {auth['access_token']}"}


async def verify_user(email: str, *, grant: str | None = None, trial_ends_in: timedelta | None = None) -> None:
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        user.email_verified = True
        user.trial_ends_at = datetime.now(UTC) + trial_ends_in if trial_ends_in else None
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


def test_admin_users_include_the_number_of_posts_created(client, monkeypatch):
    settings = get_settings()
    admin_email = "reverb-admin@example.com"
    creator_email = "posts-created@example.com"
    monkeypatch.setattr(settings, "admin_emails", admin_email)
    _, admin_headers = register(client, admin_email)
    _, creator_headers = register(client, creator_email)
    asyncio.run(verify_user(creator_email))
    create_draft(client, creator_headers, "first-admin-post")
    create_draft(client, creator_headers, "second-admin-post")

    response = client.get("/v1/admin/users", headers=admin_headers)

    assert response.status_code == 200, response.text
    users = {item["email"]: item for item in response.json()["items"]}
    assert users[creator_email]["post_count"] == 2
    assert users[admin_email]["post_count"] == 0


def test_email_verification_starts_seven_day_pro_trial(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    _, headers = register(client, "new-trial@example.com")

    unverified = client.get("/v1/billing/me", headers=headers)
    assert unverified.json()["tier"] == "expired"

    async def verification_token() -> str:
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "new-trial@example.com"))
            token = await create_one_time_token(db, user, "verify_email", 60)
            await db.commit()
            return token

    token = asyncio.run(verification_token())
    assert client.post("/v1/auth/verify", json={"token": token}).status_code == 200

    access = client.get("/v1/billing/me", headers=headers).json()
    assert access["tier"] == "trial"
    assert access["plan"] == "pro"
    assert all(access["capabilities"][name] for name in ("publish", "schedule", "analytics", "ai_creation"))
    ends = datetime.fromisoformat(access["access_until"])
    assert timedelta(days=6, hours=23) < ends - datetime.now(UTC) <= timedelta(days=7)

    # Verifying again must never extend the trial.
    async def reverify() -> datetime:
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "new-trial@example.com"))
            before = user.trial_ends_at
            start_trial(user)
            assert user.trial_ends_at == before
            return before

    asyncio.run(reverify())


def test_trial_allows_unlimited_posts_and_scheduling(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    email = "trial-posting@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email, trial_ends_in=timedelta(days=7)))
    first = create_draft(client, headers, "trial-first")
    second = create_draft(client, headers, "trial-second")
    monkeypatch.setattr("app.posts.publish_post.delay", lambda _post_id: None)

    assert client.post(f"/v1/posts/{first['id']}/publish", headers=headers, json={"mode": "now"}).status_code == 202
    scheduled = client.post(
        f"/v1/posts/{second['id']}/publish",
        headers=headers,
        json={
            "mode": "scheduled",
            "scheduled_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "timezone": "Africa/Lagos",
        },
    )
    assert scheduled.status_code == 202, scheduled.text


def test_expired_trial_blocks_publishing(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    email = "expired-trial@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email, trial_ends_in=timedelta(days=1)))
    draft = create_draft(client, headers, "expired-trial")

    async def expire_trial() -> None:
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == email))
            user.trial_ends_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()

    asyncio.run(expire_trial())

    access = client.get("/v1/billing/me", headers=headers).json()
    assert access["tier"] == "expired"
    denied = client.post(f"/v1/posts/{draft['id']}/publish", headers=headers, json={"mode": "now"})
    assert denied.status_code == 403
    assert denied.json()["code"] == "upgrade_required"


def test_pro_trial_outranks_basic_grant(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    email = "trial-with-basic-grant@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email, grant="basic", trial_ends_in=timedelta(days=3)))

    access = client.get("/v1/billing/me", headers=headers).json()
    assert access["tier"] == "trial"
    assert access["capabilities"]["analytics"] is True


def test_admin_trial_reset_restarts_seven_days(client, monkeypatch):
    settings = get_settings()
    admin_email = "trial-admin@example.com"
    creator_email = "trial-reset@example.com"
    monkeypatch.setattr(settings, "admin_emails", admin_email)
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    _, admin_headers = register(client, admin_email)
    creator, creator_headers = register(client, creator_email)
    asyncio.run(verify_user(creator_email, trial_ends_in=timedelta(seconds=-1)))
    assert client.get("/v1/billing/me", headers=creator_headers).json()["tier"] == "expired"

    reset = client.post(
        f"/v1/admin/users/{creator['user']['id']}/trial-reset",
        headers=admin_headers,
        json={"reason": "Support goodwill"},
    )

    assert reset.status_code == 200, reset.text
    assert client.get("/v1/billing/me", headers=creator_headers).json()["tier"] == "trial"


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
        json={"post_id": post["id"], "generation_context": f"  {context}  ", "adjustments": ["More engaging", "shorter"]},
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["status"] == "queued"
    assert queued.json()["media_id"] == post["media"]["id"]
    assert queued.json()["generation_context"] == context
    latest = client.get(f"/v1/ai/generations/latest?post_id={post['id']}", headers=headers)
    assert latest.status_code == 200, latest.text
    assert latest.json()["id"] == queued.json()["id"]
    assert latest.json()["generation_context"] == context
    assert queued.json()["adjustments"] == ["more_engaging", "shorter"]
    assert latest.json()["adjustments"] == ["more_engaging", "shorter"]
    duplicate = client.post("/v1/ai/generations", headers=headers, json={"post_id": post["id"]})
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "ai_generation_in_progress"

    replacement_media = client.post(
        "/v1/media",
        headers=headers,
        json={"filename": "replacement.mp4", "mime_type": "video/mp4", "size_bytes": 8},
    )
    assert replacement_media.status_code == 201, replacement_media.text
    replacement_id = replacement_media.json()["id"]
    asyncio.run(mark_ready([replacement_id]))
    replaced = client.patch(
        f"/v1/posts/{post['id']}",
        headers=headers,
        json={
            "media_id": replacement_id,
            "title": post["title"],
            "caption": post["caption"],
            "hashtags": post["hashtags"],
            "versions": post["versions"],
        },
    )
    assert replaced.status_code == 200, replaced.text
    latest_after_replacement = client.get(
        f"/v1/ai/generations/latest?post_id={post['id']}", headers=headers
    )
    assert latest_after_replacement.status_code == 200
    assert latest_after_replacement.json() is None

    too_long = client.post(
        "/v1/ai/generations",
        headers=headers,
        json={"post_id": post["id"], "generation_context": "x" * 2001},
    )
    assert too_long.status_code == 422
    assert too_long.json()["code"] == "validation_error"


def test_ai_adjustment_rejects_candidate_from_replaced_video(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")
    email = "stale-ai@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email, grant="pro"))
    post = create_draft(client, headers, "stale-source")
    monkeypatch.setattr("app.tasks.run_ai_generation.delay", lambda _job_id: None)
    queued = client.post("/v1/ai/generations", headers=headers, json={"post_id": post["id"]})
    assert queued.status_code == 202, queued.text

    async def complete_job() -> None:
        async with SessionLocal() as db:
            job = await db.get(AIGenerationJob, queued.json()["id"])
            job.status = "completed"
            job.candidate = {
                "shared_caption": "Caption",
                "hashtags": [],
                "tiktok_caption": "Caption",
                "instagram_caption": "Caption",
                "facebook_title": "Facebook title",
                "facebook_caption": "Caption",
                "youtube_title": "Title",
                "youtube_description": "Description",
            }
            await db.commit()

    asyncio.run(complete_job())
    replacement_media = client.post(
        "/v1/media",
        headers=headers,
        json={"filename": "new-stale-source.mp4", "mime_type": "video/mp4", "size_bytes": 8},
    )
    assert replacement_media.status_code == 201, replacement_media.text
    replacement_id = replacement_media.json()["id"]
    asyncio.run(mark_ready([replacement_id]))
    replaced = client.patch(
        f"/v1/posts/{post['id']}",
        headers=headers,
        json={
            "media_id": replacement_id,
            "title": post["title"],
            "caption": post["caption"],
            "hashtags": post["hashtags"],
            "versions": post["versions"],
        },
    )
    assert replaced.status_code == 200, replaced.text
    adjustment = client.post(
        f"/v1/ai/generations/{queued.json()['id']}/adjust",
        headers=headers,
        json={"adjustment": "shorter"},
    )
    assert adjustment.status_code == 409
    assert adjustment.json()["code"] == "ai_candidate_stale"


def test_ai_regeneration_reuses_video_observations_without_fetching_media(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")
    email = "reusable-ai@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email, grant="pro"))
    post = create_draft(client, headers, "reusable-video")
    monkeypatch.setattr("app.tasks.run_ai_generation.delay", lambda _job_id: None)
    original = client.post("/v1/ai/generations", headers=headers, json={"post_id": post["id"]})
    assert original.status_code == 202, original.text

    candidate = {
        "shared_caption": "A creator shows a blue bag.",
        "hashtags": ["handmade"],
        "tiktok_caption": "",
        "instagram_caption": "A creator shows a blue bag.",
        "facebook_title": "",
        "facebook_caption": "",
        "youtube_title": "",
        "youtube_description": "",
    }
    observations = {
        "summary": "A creator shows a bag.",
        "details": ["Blue stitching is visible."],
        "uncertainties": [],
    }

    async def complete_original() -> None:
        async with SessionLocal() as db:
            job = await db.get(AIGenerationJob, original.json()["id"])
            job.status = "completed"
            job.candidate = candidate
            await db.commit()

    asyncio.run(complete_original())
    unavailable = client.post(
        f"/v1/ai/generations/{original.json()['id']}/adjust",
        headers=headers,
        json={"adjustment": "regenerate"},
    )
    assert unavailable.status_code == 409
    assert unavailable.json()["code"] == "ai_observations_unavailable"

    async def add_observations() -> None:
        async with SessionLocal() as db:
            job = await db.get(AIGenerationJob, original.json()["id"])
            job.video_observations = observations
            await db.commit()

    asyncio.run(add_observations())
    variant = client.post(
        f"/v1/ai/generations/{original.json()['id']}/adjust",
        headers=headers,
        json={"adjustment": "regenerate", "generation_context": "  Mention the stitching.  "},
    )
    assert variant.status_code == 202, variant.text
    assert variant.json()["kind"] == "adjustment"
    assert variant.json()["generation_context"] == "Mention the stitching."
    assert variant.json()["video_observations"] == observations

    calls = []

    class TextOnlyGemini:
        def __init__(self, on_stage=None):
            self.on_stage = on_stage

        async def adjust_candidate(self, previous, adjustment, context, **kwargs):
            calls.append((previous, adjustment, context, kwargs))
            return {**candidate, "instagram_caption": "See the blue stitching on this handmade bag."}, {}

    monkeypatch.setattr("app.gemini.GeminiClient", TextOnlyGemini)
    monkeypatch.setattr("app.tasks.materialize_object", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Video was fetched")))
    asyncio.run(_run_ai_generation(variant.json()["id"]))

    completed = client.get(f"/v1/ai/generations/{variant.json()['id']}", headers=headers)
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["video_observations"] == observations
    assert calls[0][1:3] == ("regenerate", "Mention the stitching.")
    assert calls[0][3]["video_observations"] == observations


def test_first_ai_video_job_saves_observations_for_future_variants(client, monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_enforcement_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-gemini-key")
    email = "first-pass-ai@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email, grant="pro"))
    post = create_draft(client, headers, "first-pass-video")
    monkeypatch.setattr("app.tasks.run_ai_generation.delay", lambda _job_id: None)
    queued = client.post("/v1/ai/generations", headers=headers, json={"post_id": post["id"]})
    assert queued.status_code == 202, queued.text

    observations = {
        "summary": "A creator shows a handmade bag.",
        "details": ["The bag has blue stitching."],
        "uncertainties": [],
    }
    candidate = {
        "shared_caption": "A handmade bag.",
        "hashtags": [],
        "tiktok_caption": "",
        "instagram_caption": "A handmade bag with blue stitching.",
        "facebook_title": "",
        "facebook_caption": "",
        "youtube_title": "",
        "youtube_description": "",
    }
    deleted_files = []

    @asynccontextmanager
    async def fake_materialize_object(**_kwargs):
        yield tmp_path / "fake-video.mp4"

    class FirstPassGemini:
        def __init__(self, on_stage=None):
            self.on_stage = on_stage

        async def upload_file(self, _path, _mime_type):
            return {"name": "files/test-video", "uri": "files/test-video"}

        async def create_candidate(self, **_kwargs):
            return candidate, observations, {"promptTokenCount": 120, "candidatesTokenCount": 80}

        async def delete_file(self, name):
            deleted_files.append(name)

    monkeypatch.setattr("app.tasks.materialize_object", fake_materialize_object)
    monkeypatch.setattr("app.gemini.GeminiClient", FirstPassGemini)
    asyncio.run(_run_ai_generation(queued.json()["id"]))

    completed = client.get(f"/v1/ai/generations/{queued.json()['id']}", headers=headers)
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["video_observations"] == observations
    assert completed.json()["candidate"] == candidate
    assert deleted_files == ["files/test-video"]



@pytest.mark.parametrize("initial_styles", [[], ["more_engaging", "shorter"]])
def test_worker_repairs_x_then_enforces_shorter_without_refetching_video(client, monkeypatch, tmp_path, initial_styles):
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key")
    email = f"repair-copy-{len(initial_styles)}@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email, grant="pro"))
    post = create_draft(client, headers, "repair-copy")
    edited = client.patch(f"/v1/posts/{post['id']}", headers=headers, json={
        "media_id": post["media"]["id"], "caption": "Manual caption", "hashtags": [],
        "content_format_version": 2,
        "versions": [{"platform": "x", "caption": "Manual X", "use_shared_caption": False},
                     {"platform": "instagram", "caption": "Manual Instagram", "use_shared_caption": False}],
    })
    assert edited.status_code == 200, edited.text
    monkeypatch.setattr("app.tasks.run_ai_generation.delay", lambda _job_id: None)
    generated = {"shared_caption": "A handmade bag with blue stitching.", "hashtags": ["Handmade"],
                 "x_caption": "See the blue stitching on this handmade bag. " * 10,
                 "instagram_caption": "A handmade bag with blue stitching."}
    uploads = []
    repairs = []

    class FakeGemini:
        def __init__(self, on_stage=None):
            pass

        async def upload_file(self, *args):
            uploads.append(args)
            return {"name": "files/copy-test", "uri": "files/copy-test"}

        async def create_candidate(self, **kwargs):
            return generated, {"summary": "A handmade bag.", "details": [], "uncertainties": []}, {}

        async def adjust_candidate(self, *args, **kwargs):
            return {**generated, "shared_caption": "See this handmade bag with blue stitching and a handle for carrying it."}, {}

        async def repair_copy(self, captions, limits):
            repairs.append(limits)
            return dict.fromkeys(captions, "A handmade bag with blue stitching."), {}

        async def delete_file(self, name):
            pass

    @asynccontextmanager
    async def materialize(**kwargs):
        yield tmp_path / "video.mp4"

    monkeypatch.setattr("app.gemini.GeminiClient", FakeGemini)
    monkeypatch.setattr("app.tasks.materialize_object", materialize)
    queued = client.post("/v1/ai/generations", headers=headers, json={"post_id": post["id"]})
    assert queued.status_code == 202, queued.text
    asyncio.run(_run_ai_generation(queued.json()["id"]))
    completed = client.get(f"/v1/ai/generations/{queued.json()['id']}", headers=headers).json()
    assert completed["status"] == "completed", completed
    assert completed["candidate"]["instagram_caption"] == generated["instagram_caption"]
    assert list(repairs[0]) == ["x_caption"]
    shorter = client.post(f"/v1/ai/generations/{completed['id']}/adjust", headers=headers,
                          json={"adjustments": ["more_engaging", "shorter"]})
    assert shorter.status_code == 202, shorter.text
    asyncio.run(_run_ai_generation(shorter.json()["id"]))
    result = client.get(f"/v1/ai/generations/{shorter.json()['id']}", headers=headers).json()
    assert result["status"] == "completed", result
    assert len(result["candidate"]["shared_caption"].split()) <= 10
    assert len(result["candidate"]["x_caption"].split()) <= 10
    assert result["adjustments"] == ["more_engaging", "shorter"]
    assert len(uploads) == 1

    async def failed_repair(self, captions, limits):
        from app.gemini import GeminiError
        raise GeminiError("Timed out", code="gemini_generation_timeout")

    monkeypatch.setattr(FakeGemini, "repair_copy", failed_repair)
    failure = client.post(f"/v1/ai/generations/{result['id']}/adjust", headers=headers,
                          json={"adjustments": ["professional", "shorter"]})
    assert failure.status_code == 202
    asyncio.run(_run_ai_generation(failure.json()["id"]))
    failed = client.get(f"/v1/ai/generations/{failure.json()['id']}", headers=headers).json()
    assert failed["status"] == "failed"
    assert failed["candidate"] is None
    assert failed["error_code"] == "ai_shortening_timeout"
    assert failed["error_message"] == "Couldn't shorten this caption. Please try again."
    previous = client.get(f"/v1/ai/generations/latest?post_id={post['id']}&completed_only=true", headers=headers).json()
    assert previous["id"] == result["id"]
    assert previous["candidate"] == result["candidate"]
    for invalid in ({"adjustments": []}, {"adjustments": ["invalid"]},
                    {"adjustments": ["regenerate", "shorter"]},
                    {"adjustment": "casual", "adjustments": ["shorter"]}, {}):
        assert client.post(f"/v1/ai/generations/{result['id']}/adjust", headers=headers, json=invalid).status_code == 422
    unchanged = client.get(f"/v1/posts/{post['id']}", headers=headers).json()
    assert unchanged["caption"] == "Manual caption"
    assert unchanged["versions"][0]["caption"].startswith("Manual")


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
    assert normalized == {
        "exposure": 1250,
        "likes": 42,
        "comments": 7,
        "shares": None,
        "saves": None,
    }
    assert metric == "reach"
    assert label == "Accounts reached"

    missing, metric, label = normalize_metrics("youtube", {"likes": 2})
    assert missing["exposure"] is None
    assert metric is None
    assert label is None


def test_overview_uses_provider_period_series_instead_of_publication_date(client):
    email = "accurate-analytics@example.com"
    _, headers = register(client, email)
    asyncio.run(verify_user(email, grant="pro"))
    post = create_draft(client, headers, "analytics-period")

    async def seed_analytics() -> None:
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == email))
            publication = Publication(
                post_id=post["id"],
                platform="instagram",
                status="published",
                provider_request_id="analytics-request",
                published_at=datetime.now(UTC) - timedelta(days=3),
            )
            db.add(publication)
            await db.flush()
            db.add(
                PublicationMetricSnapshot(
                    publication_id=publication.id,
                    raw_metrics={"post_metrics": {"reach": 450}},
                    normalized_metrics={
                        "exposure": 450,
                        "likes": 20,
                        "comments": 4,
                        "shares": 2,
                    },
                    primary_metric="reach",
                    primary_label="Accounts reached",
                )
            )
            db.add(
                AnalyticsPeriodSnapshot(
                    user_id=user.id,
                    period_days=30,
                    start_date="2026-08-15",
                    end_date="2026-09-13",
                    raw_metrics={},
                    normalized_metrics={
                        "total_exposure": 1200,
                        "previous_total_exposure": 1000,
                        "change_percent": 20,
                        "per_platform": {"instagram": 1200},
                        "per_day": {"2026-09-10": 300, "2026-09-11": 900},
                        "engagement": {"likes": 50, "comments": 9, "shares": 3},
                        "connected_platforms": ["instagram"],
                        "reporting_platforms": ["instagram"],
                        "warnings": [],
                    },
                    provider_status="available",
                )
            )
            await db.commit()

    asyncio.run(seed_analytics())
    response = client.get("/v1/analytics/overview?period=30d", headers=headers)

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_exposure"] == 1200
    assert data["change_percent"] == 20
    assert data["trend"] == [
        {"date": "2026-09-10", "exposure": 300.0},
        {"date": "2026-09-11", "exposure": 900.0},
    ]
    assert data["engagement"]["comments"] == 9
    assert data["coverage"] == {"connected": 1, "reporting": 1}
    assert data["top_post"]["exposure"] == 450
    assert data["insights_generated_by"] == "reverb"
    assert data["insights"][0]["id"] == "momentum"
    assert data["insights"][0]["metric_value"] == "+20.0%"
    assert data["insights"][0]["confidence"] == "early_signal"


def test_ai_insight_copy_cannot_change_server_owned_facts():
    candidate = {
        "id": "momentum",
        "kind": "momentum",
        "tone": "positive",
        "headline": "Exposure is growing",
        "explanation": "Exposure increased based on the verified comparison.",
        "action": "Repeat one proven creative element.",
        "confidence": "medium",
        "metric_label": "Change vs prior period",
        "metric_value": "+24.0%",
        "supporting_post_ids": [],
    }

    merged = _merge_ai_explanations(
        [candidate],
        [
            {
                "candidate_id": "momentum",
                "explanation": "Exposure supposedly increased by 99%.",
                "action": "Publish 10 times tomorrow.",
            }
        ],
    )

    assert merged[0] == candidate


def test_post_metrics_do_not_get_overwritten_by_profile_snapshots():
    normalized, metric, _ = normalize_metrics(
        "instagram",
        {
            "post_metrics": {"reach": 240, "likes": 18, "comments": 3},
            "profile_snapshot_latest": {"reach": 80_000, "likes": 12_000},
        },
    )

    assert metric == "reach"
    assert normalized["exposure"] == 240
    assert normalized["likes"] == 18


def test_x_partial_result_retry_persistence_and_publish_validation(client, monkeypatch, tmp_path):
    from app.gemini import GeminiError

    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key")
    _, headers = register(client, "partial-x@example.com")
    asyncio.run(verify_user("partial-x@example.com", grant="pro"))
    post = create_draft(client, headers, "partial-x")
    payload = {"media_id": post["media"]["id"], "caption": "Manual shared", "hashtags": [],
               "content_format_version": 2,
               "versions": [{"platform": "x", "caption": "Manual X", "use_shared_caption": False},
                            {"platform": "instagram", "caption": "Manual Instagram", "use_shared_caption": False}]}
    assert client.patch(f"/v1/posts/{post['id']}", headers=headers, json=payload).status_code == 200
    monkeypatch.setattr("app.tasks.run_ai_generation.delay", lambda _id: None)
    generated = {"shared_caption": "A handmade bag with blue stitching.", "hashtags": ["Handmade"],
                 "x_caption": "long " * 100, "instagram_caption": "A handmade bag."}
    observations = {"summary": "A handmade bag.", "details": [], "uncertainties": []}
    uploads, repairs = [], []

    class FakeGemini:
        def __init__(self, on_stage=None):
            pass

        async def upload_file(self, *args):
            uploads.append(args)
            return {"name": "files/partial-x", "uri": "files/partial-x"}

        async def create_candidate(self, **kwargs):
            return generated, observations, {}

        async def repair_copy(self, captions, limits):
            repairs.append(captions)
            if len(repairs) < 3:
                raise GeminiError("Timed out", code="gemini_generation_timeout")
            return {"x_caption": "Blue stitching, handmade."}, {}

        async def delete_file(self, name):
            pass

    @asynccontextmanager
    async def materialize(**kwargs):
        yield tmp_path / "video.mp4"

    monkeypatch.setattr("app.gemini.GeminiClient", FakeGemini)
    monkeypatch.setattr("app.tasks.materialize_object", materialize)
    queued = client.post("/v1/ai/generations", headers=headers, json={"post_id": post["id"]})
    assert queued.status_code == 202
    asyncio.run(_run_ai_generation(queued.json()["id"]))
    latest_url = f"/v1/ai/generations/latest?post_id={post['id']}&completed_only=true"
    partial = client.get(latest_url, headers=headers).json()
    assert partial["status"] == "completed"
    assert partial["candidate"]["platform_errors"]["x"]["code"] == "ai_shortening_timeout"
    assert partial["candidate"]["instagram_caption"] == generated["instagram_caption"]
    assert partial["video_observations"] == observations
    for attempt in range(2):
        response = client.post(f"/v1/ai/generations/{partial['id']}/adjust", headers=headers,
                               json={"adjustment": "retry_x"})
        assert response.status_code == 202, response.text
        asyncio.run(_run_ai_generation(response.json()["id"]))
        partial = client.get(latest_url, headers=headers).json()
        assert partial["status"] == "completed", partial
        assert partial["candidate"]["instagram_caption"] == generated["instagram_caption"]
        assert partial["video_observations"] == observations
        assert list(repairs[-1]) == ["x_caption"]
        if attempt == 0:
            assert partial["candidate"]["platform_errors"]["x"]
    assert "platform_errors" not in partial["candidate"]
    assert partial["candidate"]["x_caption"] == "Blue stitching, handmade."
    assert len(uploads) == 1
    assert client.post(f"/v1/ai/generations/{partial['id']}/adjust", headers=headers,
                       json={"adjustment": "retry_x"}).status_code == 409
    assert client.post(f"/v1/ai/generations/{partial['id']}/adjust", headers=headers,
                       json={"adjustments": ["retry_x", "shorter"]}).status_code == 422
    unchanged = client.get(f"/v1/posts/{post['id']}", headers=headers).json()
    assert unchanged["caption"] == "Manual shared"
    assert next(v for v in unchanged["versions"] if v["platform"] == "x")["caption"] == "Manual X"
    # Drafts can be edited freely; publishing validation remains mandatory.
    payload["versions"][0]["caption"] = "long " * 100
    patched = client.patch(f"/v1/posts/{post['id']}", headers=headers, json=payload)
    assert patched.status_code == 200, patched.text
    publish = client.post(f"/v1/posts/{post['id']}/publish", headers=headers, json={})
    assert publish.status_code in (409, 422), publish.text


def test_initial_generation_validates_styles():
    from pydantic import ValidationError

    from app.ai import GenerateIn

    assert GenerateIn(post_id="draft").adjustments == []
    assert GenerateIn(post_id="draft", adjustments=[" Casual ", "casual"]).adjustments == ["casual"]
    for choices in [["unknown"], ["retry_x"], ["regenerate"], ["shorter"] * 5]:
        with pytest.raises(ValidationError):
            GenerateIn(post_id="draft", adjustments=choices)
