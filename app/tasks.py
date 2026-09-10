from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from celery import Celery
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import get_settings
from .database import TaskSessionLocal
from .events import publish_post_event
from .models import MediaAsset, Post, Publication, PublicationAttempt, User, utcnow
from .notifications import deliver_pending_notifications, queue_terminal_notification
from .providers import (
    ProviderError,
    UploadPostClient,
    upload_idempotency_key,
    upload_request_id,
)

settings = get_settings()
UNCLAIMED_QUEUE_TIMEOUT = timedelta(minutes=1)
CLAIMED_QUEUE_TIMEOUT = timedelta(minutes=15)
MAX_SUBMISSION_ATTEMPTS = 4
logger = logging.getLogger(__name__)
celery_app = Celery("marble", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "reconcile-active-posts": {"task": "marble.reconcile_active", "schedule": 10.0},
        "cleanup-expired-media": {"task": "marble.cleanup_media", "schedule": 3600.0},
        "deliver-post-notifications": {
            "task": "marble.deliver_post_notifications",
            "schedule": 30.0,
        },
    },
)


def derive_post_status(post: Post) -> str:
    statuses = [item.status for item in post.publications]
    if not statuses:
        return post.status
    if all(status == "scheduled" for status in statuses):
        return "scheduled"
    if any(status in {"scheduled", "queued", "publishing"} for status in statuses):
        return "publishing"
    if all(status == "published" for status in statuses):
        return "published"
    if all(status in {"failed", "action_required"} for status in statuses):
        return "failed"
    return "partially_published"


def post_state_signature(post: Post) -> tuple:
    return (
        post.status,
        post.provider_request_id,
        post.provider_job_id,
        tuple(
            sorted(
                (
                    item.platform,
                    item.status,
                    item.url,
                    item.error_code,
                    item.error_message,
                    item.fallback_to_inbox,
                )
                for item in post.publications
            )
        ),
    )


def mark_connections_used(post: Post, platforms: set[str]) -> None:
    used_at = utcnow()
    for connection in post.user.connections:
        if connection.platform in platforms:
            connection.last_used_at = used_at


def provider_result_status(result: dict) -> str:
    if result.get("fallback_to_inbox"):
        return "action_required"
    raw = str(result.get("status", "")).lower()
    message = str(result.get("message", "")).lower()
    if raw == "completed":
        return "published"
    # Upload-Post can report success=false while a platform result is
    # retryable. That state is non-terminal: the provider may still complete
    # the original request, so exposing a retry here can create a duplicate.
    if raw in {"processing", "in_progress", "retryable"}:
        return "publishing"
    if raw in {"pending", "queued"}:
        return "queued"
    if result.get("skipped") or raw == "failed" or result.get("success") is False:
        return "failed"
    # Some status responses use success=true to mean that the platform job was
    # accepted, while their message still says it is queued or processing.
    if result.get("success") is True:
        if "queue" in message or "pending" in message:
            return "queued"
        if "processing" in message or "progress" in message:
            return "publishing"
        return "published"
    return "queued"


def normalized_results(payload: dict) -> list[dict]:
    single_result = payload.get("result")
    if isinstance(single_result, dict) and payload.get("platform"):
        return [{"platform": payload["platform"], **single_result}]
    results = payload.get("results") or []
    if isinstance(results, dict):
        return [
            {"platform": platform, **(value if isinstance(value, dict) else {})}
            for platform, value in results.items()
        ]
    return [value for value in results if isinstance(value, dict)]


async def apply_provider_payload(post: Post, payload: dict) -> None:
    source_request_id = payload.get("request_id")
    source_job_id = payload.get("job_id")
    by_platform = {item.platform: item for item in post.publications}
    for result in normalized_results(payload):
        platform = result.get("platform")
        publication = by_platform.get(platform)
        if not publication:
            continue
        next_status = provider_result_status(result)
        source_is_current = (
            (not source_request_id or publication.provider_request_id == source_request_id)
            and (not source_job_id or publication.provider_job_id == source_job_id)
        )
        # A late callback from an older attempt may confirm success, but it must
        # never demote a newer attempt or a post that is already live.
        if next_status != "published" and (
            publication.status == "published" or not source_is_current
        ):
            continue
        publication.status = next_status
        if next_status == "published":
            if source_request_id:
                publication.provider_request_id = source_request_id
            if source_job_id:
                publication.provider_job_id = source_job_id
        publication.url = result.get("url") or result.get("post_url") or publication.url
        publication.provider_post_id = (
            str(result.get("post_id") or result.get("video_id") or result.get("publish_id") or "") or None
        )
        publication.error_code = result.get("error_code") or result.get("skip_reason")
        publication.error_message = (
            (result.get("error") or result.get("message"))
            if publication.status == "failed"
            else None
        )
        publication.fallback_to_inbox = bool(result.get("fallback_to_inbox"))
        if publication.status == "published" and publication.published_at is None:
            publication.published_at = utcnow()
    post.status = derive_post_status(post)
    if post.status == "published":
        post.media.delete_after = utcnow() + timedelta(hours=24)
    elif post.status in {"failed", "partially_published"}:
        post.media.delete_after = utcnow() + timedelta(days=7)


async def record_provider_attempt(
    db: AsyncSession,
    publication: Publication,
    *,
    kind: str,
    request_id: str | None,
    job_id: str | None,
    idempotency_key: str,
    status: str,
) -> None:
    existing = await db.scalar(
        select(PublicationAttempt).where(
            PublicationAttempt.publication_id == publication.id,
            PublicationAttempt.idempotency_key == idempotency_key,
        )
    )
    if existing:
        existing.provider_request_id = request_id or existing.provider_request_id
        existing.provider_job_id = job_id or existing.provider_job_id
        existing.status = status
        return
    db.add(
        PublicationAttempt(
            publication_id=publication.id,
            kind=kind,
            status=status,
            provider_request_id=request_id,
            provider_job_id=job_id,
            idempotency_key=idempotency_key,
        )
    )


async def _publish_post(post_id: str) -> None:
    async with TaskSessionLocal() as db:
        post = await db.scalar(
            select(Post)
            .where(Post.id == post_id)
            .options(
                selectinload(Post.user).selectinload(User.connections),
                selectinload(Post.media),
                selectinload(Post.versions),
                selectinload(Post.publications),
            )
            .with_for_update()
        )
        if not post or post.status not in {"queued", "scheduled", "publishing"}:
            return
        if post.provider_request_id or post.provider_job_id:
            return
        for publication in post.publications:
            publication.status = "scheduled" if post.publish_mode == "scheduled" else "queued"
            publication.attempts += 1
        post.updated_at = utcnow()
        await db.commit()

        facebook_page_id = (
            next((item.target_page_id for item in post.user.connections if item.platform == "facebook"), None)
            if post.user.connections
            else None
        )
        expected_request_id = upload_request_id(post.id, post.schedule_revision)
        idempotency_key = upload_idempotency_key(post.id, post.schedule_revision)
        try:
            response = await UploadPostClient().publish_video(
                profile=post.user.upload_post_profile,
                video_path=Path(post.media.storage_path),
                post_id=post.id,
                revision=post.schedule_revision,
                versions=[
                    {
                        "platform": item.platform,
                        "caption": item.caption,
                        "title": item.title,
                        "options": item.options,
                    }
                    for item in post.versions
                ],
                scheduled_at=post.scheduled_at_utc.isoformat() if post.scheduled_at_utc else None,
                timezone=post.schedule_timezone,
                facebook_page_id=facebook_page_id,
                request_id=expected_request_id,
            )
            post.provider_request_id = response.get("request_id") or expected_request_id
            post.provider_job_id = response.get("job_id")
            if post.publish_mode == "scheduled" and not post.provider_job_id:
                raise ProviderError(
                    "provider_invalid_response",
                    "Upload-Post did not return a scheduled job ID",
                )
            for publication in post.publications:
                publication.provider_request_id = post.provider_request_id
                publication.provider_job_id = post.provider_job_id
                await record_provider_attempt(
                    db,
                    publication,
                    kind="initial",
                    request_id=post.provider_request_id,
                    job_id=post.provider_job_id,
                    idempotency_key=idempotency_key,
                    status="submitted",
                )
            mark_connections_used(post, {version.platform for version in post.versions})
            if post.publish_mode == "scheduled":
                post.status = "scheduled"
                post.media.delete_after = None
            else:
                post.status = "publishing"
                await apply_provider_payload(post, response)
            await queue_terminal_notification(db, post)
            await db.commit()
            await publish_post_event(post.user_id, post.id)
        except ProviderError as exc:
            post.provider_request_id = expected_request_id
            for publication in post.publications:
                publication.provider_request_id = expected_request_id
                publication.status = "failed"
                publication.error_code = exc.code
                publication.error_message = str(exc)
                await record_provider_attempt(
                    db,
                    publication,
                    kind="initial",
                    request_id=expected_request_id,
                    job_id=None,
                    idempotency_key=idempotency_key,
                    status="unknown" if exc.code == "provider_unavailable" else "failed",
                )
            post.status = "draft" if post.publish_mode == "scheduled" else "failed"
            post.media.delete_after = utcnow() + timedelta(days=7)
            await queue_terminal_notification(db, post)
            await db.commit()
            await publish_post_event(post.user_id, post.id)


@celery_app.task(
    name="marble.publish_post",
    autoretry_for=(OSError, OperationalError),
    retry_backoff=True,
    max_retries=3,
)
def publish_post(post_id: str) -> None:
    asyncio.run(_publish_post(post_id))


async def _reconcile_post(post_id: str) -> None:
    async with TaskSessionLocal() as db:
        post = await db.scalar(
            select(Post)
            .where(Post.id == post_id)
            .options(selectinload(Post.media), selectinload(Post.publications))
        )
        if not post or not (post.provider_request_id or post.provider_job_id):
            return
        before = post_state_signature(post)
        try:
            payload = await UploadPostClient().status(
                request_id=post.provider_request_id, job_id=post.provider_job_id
            )
        except ProviderError:
            return
        await apply_provider_payload(post, payload)
        individual = {
            item.provider_request_id
            for item in post.publications
            if item.provider_request_id and item.provider_request_id != post.provider_request_id
        }
        for request_id in individual:
            try:
                retry_payload = await UploadPostClient().status(request_id=request_id)
            except ProviderError:
                continue
            await apply_provider_payload(post, retry_payload)
        await queue_terminal_notification(db, post)
        await db.commit()
        if post_state_signature(post) != before:
            await publish_post_event(post.user_id, post.id)


@celery_app.task(name="marble.reconcile_post")
def reconcile_post(post_id: str) -> None:
    asyncio.run(_reconcile_post(post_id))


async def _retry_platform(post_id: str, platform: str) -> None:
    async with TaskSessionLocal() as db:
        post = await db.scalar(
            select(Post)
            .where(Post.id == post_id)
            .options(
                selectinload(Post.user).selectinload(User.connections),
                selectinload(Post.media),
                selectinload(Post.versions),
                selectinload(Post.publications),
            )
        )
        if not post:
            return
        publication = next((item for item in post.publications if item.platform == platform), None)
        version = next((item for item in post.versions if item.platform == platform), None)
        if not publication or not version or publication.status != "queued":
            return
        client = UploadPostClient()
        original_request_id = publication.provider_request_id or post.provider_request_id
        original_job_id = publication.provider_job_id or post.provider_job_id
        provider_confirmed_missing = False

        # A local failure can be a false negative. Always ask the provider what
        # happened before creating any new social post.
        if original_request_id or original_job_id:
            try:
                status_payload = await client.status(
                    request_id=original_request_id, job_id=original_job_id
                )
            except ProviderError as exc:
                if exc.status_code == 404:
                    provider_confirmed_missing = True
                else:
                    publication.status = "failed"
                    publication.error_code = "retry_status_unavailable"
                    publication.error_message = (
                        "Could not verify the original post, so Reverb did not risk posting it twice"
                    )
                    post.status = derive_post_status(post)
                    await queue_terminal_notification(db, post)
                    await db.commit()
                    await publish_post_event(post.user_id, post.id)
                    return
            else:
                await apply_provider_payload(post, status_payload)
                target_result = next(
                    (
                        result
                        for result in normalized_results(status_payload)
                        if result.get("platform") == platform
                    ),
                    None,
                )
                target_status = (
                    provider_result_status(target_result)
                    if target_result
                    else str(status_payload.get("status", "")).lower()
                )
                if target_status == "completed":
                    target_status = "published"
                elif target_status in {"pending", "queued"}:
                    target_status = "queued"
                elif target_status in {"processing", "in_progress"}:
                    target_status = "publishing"
                if target_status == "published" or publication.status == "published":
                    publication.status = "published"
                    publication.error_code = publication.error_message = None
                    post.status = derive_post_status(post)
                    await queue_terminal_notification(db, post)
                    await db.commit()
                    await publish_post_event(post.user_id, post.id)
                    return
                if target_status in {"queued", "publishing"}:
                    publication.status = target_status
                    publication.error_code = publication.error_message = None
                    post.status = derive_post_status(post)
                    await queue_terminal_notification(db, post)
                    await db.commit()
                    await publish_post_event(post.user_id, post.id)
                    return

                failed_siblings = [
                    item
                    for item in post.publications
                    if item.status in {"failed", "action_required"}
                    and (
                        (original_request_id and item.provider_request_id == original_request_id)
                        or (original_job_id and item.provider_job_id == original_job_id)
                    )
                ]
                if target_status in {"failed", "action_required"} and len(
                    failed_siblings
                ) <= 1:
                    retry_key = (
                        f"marble-provider-retry:{post.id}:{platform}:{post.schedule_revision}"
                    )
                    try:
                        await client.retry(
                            request_id=original_request_id,
                            job_id=original_job_id,
                            idempotency_key=retry_key,
                        )
                    except ProviderError as exc:
                        publication.status = "failed"
                        publication.error_code = exc.code
                        publication.error_message = str(exc)
                    else:
                        publication.status = "queued"
                        publication.error_code = publication.error_message = None
                        publication.attempts += 1
                        mark_connections_used(post, {platform})
                        await record_provider_attempt(
                            db,
                            publication,
                            kind="provider_retry",
                            request_id=original_request_id,
                            job_id=original_job_id,
                            idempotency_key=retry_key,
                            status="submitted",
                        )
                    post.status = derive_post_status(post)
                    await queue_terminal_notification(db, post)
                    await db.commit()
                    await publish_post_event(post.user_id, post.id)
                    return

        # Legacy attempts without a provider reference, confirmed-missing
        # attempts, and a selected destination among multiple failures need a
        # platform-only request. This happens only after reconciliation above.
        publication.status = "publishing"
        publication.attempts += 1
        retry_post_id = f"{post.id}:{platform}:retry"
        expected_request_id = upload_request_id(retry_post_id, post.schedule_revision)
        retry_key = upload_idempotency_key(retry_post_id, post.schedule_revision)
        publication.provider_request_id = expected_request_id
        publication.provider_job_id = None
        await db.commit()
        await publish_post_event(post.user_id, post.id)
        facebook_page_id = next(
            (item.target_page_id for item in post.user.connections if item.platform == "facebook"), None
        )
        try:
            payload = await client.publish_video(
                profile=post.user.upload_post_profile,
                video_path=Path(post.media.storage_path),
                post_id=retry_post_id,
                revision=post.schedule_revision,
                versions=[
                    {
                        "platform": version.platform,
                        "caption": version.caption,
                        "title": version.title,
                        "options": version.options,
                    }
                ],
                scheduled_at=None,
                timezone=None,
                facebook_page_id=facebook_page_id,
                request_id=expected_request_id,
            )
            publication.provider_request_id = payload.get("request_id") or expected_request_id
            mark_connections_used(post, {platform})
            await record_provider_attempt(
                db,
                publication,
                kind="targeted_retry" if not provider_confirmed_missing else "missing_retry",
                request_id=publication.provider_request_id,
                job_id=None,
                idempotency_key=retry_key,
                status="submitted",
            )
            await apply_provider_payload(post, payload)
            if publication.provider_request_id and publication.status not in {
                "published",
                "failed",
                "action_required",
            }:
                publication.status = "queued"
        except ProviderError as exc:
            publication.status = "failed"
            publication.error_code = exc.code
            publication.error_message = str(exc)
            await record_provider_attempt(
                db,
                publication,
                kind="targeted_retry" if not provider_confirmed_missing else "missing_retry",
                request_id=expected_request_id,
                job_id=None,
                idempotency_key=retry_key,
                status="unknown" if exc.code == "provider_unavailable" else "failed",
            )
        post.status = derive_post_status(post)
        await queue_terminal_notification(db, post)
        await db.commit()
        await publish_post_event(post.user_id, post.id)


@celery_app.task(
    name="marble.retry_platform",
    autoretry_for=(OSError, OperationalError),
    retry_backoff=True,
    max_retries=3,
)
def retry_platform(post_id: str, platform: str) -> None:
    asyncio.run(_retry_platform(post_id, platform))


async def _reconcile_active() -> None:
    now = utcnow()
    async with TaskSessionLocal() as db:
        posts = list(
            await db.scalars(
                select(Post)
                .where(Post.status.in_(["scheduled", "publishing", "queued"]))
                .options(selectinload(Post.media), selectinload(Post.publications))
            )
        )
        reconcile_ids: list[str] = []
        redispatch_ids: list[str] = []
        failed_posts: list[tuple[str, str]] = []
        for post in posts:
            if post.provider_request_id or post.provider_job_id:
                reconcile_ids.append(post.id)
                continue
            attempts = max((item.attempts for item in post.publications), default=0)
            timeout = CLAIMED_QUEUE_TIMEOUT if attempts else UNCLAIMED_QUEUE_TIMEOUT
            updated_at = post.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if updated_at <= now - timeout:
                if attempts >= MAX_SUBMISSION_ATTEMPTS:
                    for publication in post.publications:
                        if publication.status not in {"published", "failed", "action_required"}:
                            publication.status = "failed"
                            publication.error_code = "submission_worker_failed"
                            publication.error_message = (
                                "Reverb could not hand this post to the publishing provider after several attempts"
                            )
                    post.status = derive_post_status(post)
                    post.media.delete_after = now + timedelta(days=7)
                    await queue_terminal_notification(db, post)
                    failed_posts.append((post.user_id, post.id))
                    continue
                # Advancing updated_at acts as a dispatch lease so the beat job
                # cannot flood Redis while this recovery attempt is waiting.
                post.updated_at = now
                redispatch_ids.append(post.id)
        await db.commit()
    for post_id in reconcile_ids:
        reconcile_post.delay(post_id)
    for post_id in redispatch_ids:
        logger.warning("Redispatching stale provider submission for post %s", post_id)
        publish_post.delay(post_id)
    for user_id, post_id in failed_posts:
        await publish_post_event(user_id, post_id)


@celery_app.task(name="marble.reconcile_active")
def reconcile_active() -> None:
    asyncio.run(_reconcile_active())


async def _deliver_post_notifications() -> None:
    async with TaskSessionLocal() as db:
        await deliver_pending_notifications(db)


@celery_app.task(
    name="marble.deliver_post_notifications",
    autoretry_for=(OSError, OperationalError),
    retry_backoff=True,
    max_retries=3,
)
def deliver_post_notifications() -> None:
    asyncio.run(_deliver_post_notifications())


async def _cleanup_media() -> None:
    async with TaskSessionLocal() as db:
        assets = list(
            await db.scalars(
                select(MediaAsset).where(
                    MediaAsset.delete_after.is_not(None), MediaAsset.delete_after <= datetime.now(UTC)
                )
            )
        )
        for asset in assets:
            Path(asset.storage_path).unlink(missing_ok=True)
            post_count = await db.scalar(select(func.count(Post.id)).where(Post.media_id == asset.id))
            if post_count:
                asset.storage_path = ""
                asset.delete_after = None
            else:
                if asset.thumbnail_path:
                    Path(asset.thumbnail_path).unlink(missing_ok=True)
                await db.delete(asset)
        await db.commit()


@celery_app.task(name="marble.cleanup_media")
def cleanup_media() -> None:
    asyncio.run(_cleanup_media())
