from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery.exceptions import CeleryError
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .database import get_db
from .deps import get_current_user, require_verified_user
from .events import publish_post_event
from .media_routes import media_out
from .models import MediaAsset, PlatformVersion, Post, Publication, SocialConnection, User
from .notifications import queue_terminal_notification
from .providers import ProviderError, UploadPostClient
from .schemas import (
    BulkDeleteFailureOut,
    BulkDeleteIn,
    BulkDeleteOut,
    MessageOut,
    PostListOut,
    PostOut,
    PostUpsertIn,
    PublicationOut,
    PublishIn,
    ScheduleUpdateIn,
    VersionIn,
)
from .tasks import derive_post_status, publish_post, retry_platform

router = APIRouter(prefix="/v1/posts", tags=["posts"])
POST_OPTIONS = (
    selectinload(Post.media),
    selectinload(Post.versions),
    selectinload(Post.publications),
)


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def validate_schedule(value: datetime | None, timezone: str | None) -> datetime:
    if not value or not timezone:
        raise HTTPException(
            status_code=422,
            detail={"code": "schedule_required", "message": "Choose a date, time and timezone"},
        )
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "invalid_timezone", "message": "Use a valid IANA timezone"}
        ) from exc
    utc_value = normalize_utc(value)
    now = datetime.now(UTC)
    if utc_value < now + timedelta(minutes=10):
        raise HTTPException(
            status_code=422,
            detail={"code": "schedule_too_soon", "message": "Schedule at least 10 minutes from now"},
        )
    if utc_value > now + timedelta(days=365):
        raise HTTPException(
            status_code=422,
            detail={"code": "schedule_too_far", "message": "Schedule within the next 365 days"},
        )
    return utc_value


def post_out(post: Post) -> PostOut:
    return PostOut(
        id=post.id,
        media=media_out(post.media),
        title=post.title,
        caption=post.caption,
        hashtags=post.hashtags or [],
        status=post.status,
        publish_mode=post.publish_mode,
        scheduled_at=post.scheduled_at_utc,
        schedule_timezone=post.schedule_timezone,
        provider_job_id=post.provider_job_id,
        can_edit_schedule=post.status == "scheduled" and bool(post.provider_job_id),
        versions=[
            VersionIn(
                platform=item.platform, caption=item.caption, title=item.title, options=item.options or {}
            )
            for item in post.versions
        ],
        publications=[
            PublicationOut(
                platform=item.platform,
                status=item.status,
                attempts=item.attempts,
                url=item.url,
                error_code=item.error_code,
                error_message=item.error_message,
                fallback_to_inbox=item.fallback_to_inbox,
                published_at=item.published_at,
            )
            for item in post.publications
        ],
        created_at=post.created_at,
        updated_at=post.updated_at,
    )


async def owned_post(db: AsyncSession, user: User, post_id: str, *, lock: bool = False) -> Post:
    query = (
        select(Post)
        .where(Post.id == post_id, Post.user_id == user.id)
        .options(*POST_OPTIONS)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    post = await db.scalar(query)
    if not post:
        raise HTTPException(status_code=404, detail={"code": "post_not_found", "message": "Post not found"})
    return post


def validate_versions(
    payload: PostUpsertIn | ScheduleUpdateIn, *, for_publish: bool = False
) -> list[VersionIn]:
    versions = payload.versions or []
    if not versions:
        if for_publish:
            raise HTTPException(
                status_code=422,
                detail={"code": "platform_required", "message": "Choose at least one platform"},
            )
        return []
    platforms = [item.platform for item in versions]
    if len(platforms) != len(set(platforms)):
        raise HTTPException(
            status_code=422, detail={"code": "duplicate_platform", "message": "Each platform can appear once"}
        )
    if for_publish and any(not item.caption.strip() for item in versions):
        raise HTTPException(
            status_code=422,
            detail={"code": "caption_required", "message": "Add a caption for every platform"},
        )
    youtube = next((item for item in versions if item.platform == "youtube"), None)
    if for_publish and youtube and not (youtube.title or "").strip():
        raise HTTPException(
            status_code=422, detail={"code": "youtube_title_required", "message": "YouTube requires a title"}
        )
    return versions


async def replace_versions(db: AsyncSession, post: Post, versions: list[VersionIn]) -> None:
    await db.execute(delete(PlatformVersion).where(PlatformVersion.post_id == post.id))
    for item in versions:
        db.add(
            PlatformVersion(
                post_id=post.id,
                platform=item.platform,
                caption=item.caption.strip(),
                title=item.title.strip() if item.title else None,
                options=item.options,
            )
        )
    await db.flush()


@router.post("", response_model=PostOut, status_code=201)
async def create_post(
    payload: PostUpsertIn, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)
):
    versions = validate_versions(payload)
    media = await db.scalar(
        select(MediaAsset).where(MediaAsset.id == payload.media_id, MediaAsset.user_id == user.id)
    )
    if not media or media.status == "failed":
        raise HTTPException(
            status_code=409, detail={"code": "media_unavailable", "message": "Choose a valid video"}
        )
    post = Post(
        user_id=user.id,
        media_id=media.id,
        title=payload.title.strip(),
        caption=payload.caption.strip(),
        hashtags=payload.hashtags,
    )
    db.add(post)
    await db.flush()
    await replace_versions(db, post, versions)
    await db.commit()
    return post_out(await owned_post(db, user, post.id))


@router.patch("/{post_id}", response_model=PostOut)
async def update_post(
    post_id: str,
    payload: PostUpsertIn,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    versions = validate_versions(payload)
    post = await owned_post(db, user, post_id, lock=True)
    if post.status not in {"draft", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "post_locked", "message": "Use schedule editing for a scheduled post"},
        )
    if post.media_id != payload.media_id:
        media = await db.scalar(
            select(MediaAsset).where(
                MediaAsset.id == payload.media_id,
                MediaAsset.user_id == user.id,
                MediaAsset.status != "failed",
            )
        )
        if not media:
            raise HTTPException(
                status_code=409,
                detail={"code": "media_not_ready", "message": "Finish uploading the video first"},
            )
        post.media_id = media.id
    post.title, post.caption, post.hashtags = payload.title.strip(), payload.caption.strip(), payload.hashtags
    post.status = "draft"
    await replace_versions(db, post, versions)
    await db.commit()
    return post_out(await owned_post(db, user, post.id))


@router.get("", response_model=PostListOut)
async def list_posts(
    status: str = Query(default="all", pattern="^(all|published|failed|scheduled|draft)$"),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Post)
        .where(Post.user_id == user.id)
        .options(*POST_OPTIONS)
        .order_by(Post.created_at.desc())
        .limit(limit)
    )
    if status == "published":
        query = query.where(Post.status == "published")
    elif status == "failed":
        query = query.where(Post.status.in_(["failed", "partially_published"]))
    elif status == "scheduled":
        query = query.where(Post.status == "scheduled")
    elif status == "draft":
        query = query.where(Post.status.in_(["draft", "cancelled"]))
    posts = list(await db.scalars(query))
    return PostListOut(items=[post_out(post) for post in posts])


@router.get("/{post_id}", response_model=PostOut)
async def get_post(post_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return post_out(await owned_post(db, user, post_id))


@router.post("/{post_id}/publish", response_model=PostOut, status_code=202)
async def publish(
    post_id: str,
    payload: PublishIn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    post = await owned_post(db, user, post_id, lock=True)
    if post.status not in {"draft", "cancelled", "failed"}:
        return post_out(post)
    if post.media.status != "ready":
        raise HTTPException(
            status_code=409,
            detail={"code": "media_not_ready", "message": "Finish uploading the video first"},
        )
    validate_versions(
        PostUpsertIn(
            media_id=post.media_id,
            title=post.title,
            caption=post.caption,
            hashtags=post.hashtags or [],
            versions=[
                VersionIn(
                    platform=item.platform,
                    caption=item.caption,
                    title=item.title,
                    options=item.options or {},
                )
                for item in post.versions
            ],
        ),
        for_publish=True,
    )
    selected = {item.platform for item in post.versions}
    connections = list(
        await db.scalars(
            select(SocialConnection).where(
                SocialConnection.user_id == user.id, SocialConnection.platform.in_(selected)
            )
        )
    )
    ready = {item.platform for item in connections if item.status == "connected" and not item.reauth_required}
    missing = sorted(selected - ready)
    if missing:
        raise HTTPException(
            status_code=409,
            detail={"code": "accounts_not_connected", "message": f"Reconnect: {', '.join(missing)}"},
        )
    if payload.mode == "scheduled":
        post.scheduled_at_utc = validate_schedule(payload.scheduled_at, payload.timezone)
        post.schedule_timezone = payload.timezone
        post.status = "scheduled"
    else:
        post.scheduled_at_utc = None
        post.schedule_timezone = None
        post.status = "queued"
    post.publish_mode = payload.mode
    post.schedule_revision += 1
    await db.execute(delete(Publication).where(Publication.post_id == post.id))
    for version in post.versions:
        db.add(
            Publication(
                post_id=post.id,
                platform=version.platform,
                status="scheduled" if payload.mode == "scheduled" else "queued",
            )
        )
    await db.commit()
    try:
        publish_post.delay(post.id)
    except CeleryError as exc:
        if payload.mode == "scheduled":
            post.status = "draft"
            post.publish_mode = None
            post.scheduled_at_utc = None
            post.schedule_timezone = None
            await db.execute(delete(Publication).where(Publication.post_id == post.id))
        else:
            post.status = "failed"
            for publication in post.publications:
                publication.status = "failed"
                publication.error_code = "queue_unavailable"
                publication.error_message = "Publishing queue is unavailable"
            await queue_terminal_notification(db, post)
        await db.commit()
        await publish_post_event(user.id, post.id)
        raise HTTPException(
            status_code=503,
            detail={"code": "queue_unavailable", "message": "Publishing queue is unavailable; retry safely"},
        ) from exc
    await publish_post_event(user.id, post.id)
    return post_out(await owned_post(db, user, post.id))


@router.patch("/{post_id}/schedule", response_model=PostOut)
async def update_schedule(
    post_id: str,
    payload: ScheduleUpdateIn,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    post = await owned_post(db, user, post_id, lock=True)
    if post.status != "scheduled":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "schedule_already_started",
                "message": "This post has already started publishing",
            },
        )
    if not post.provider_job_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "schedule_submission_pending",
                "message": "The schedule is still being registered. Try again in a moment",
            },
        )
    has_content_changes = any(
        value is not None for value in (payload.title, payload.caption, payload.hashtags, payload.versions)
    )
    scheduled_at = validate_schedule(
        payload.scheduled_at or post.scheduled_at_utc, payload.timezone or post.schedule_timezone
    )
    timezone = payload.timezone or post.schedule_timezone
    client = UploadPostClient()
    try:
        if not has_content_changes:
            await client.update_schedule(
                post.provider_job_id, {"scheduled_date": scheduled_at.isoformat(), "timezone": timezone}
            )
            post.scheduled_at_utc, post.schedule_timezone = scheduled_at, timezone
        else:
            versions = (
                validate_versions(payload, for_publish=True)
                if payload.versions is not None
                else [
                    VersionIn(
                        platform=item.platform, caption=item.caption, title=item.title, options=item.options
                    )
                    for item in post.versions
                ]
            )
            await client.cancel_schedule(post.provider_job_id)
            if payload.title is not None:
                post.title = payload.title.strip()
            if payload.caption is not None:
                post.caption = payload.caption.strip()
            if payload.hashtags is not None:
                post.hashtags = payload.hashtags
            if payload.versions is not None:
                await replace_versions(db, post, versions)
            post.scheduled_at_utc, post.schedule_timezone = scheduled_at, timezone
            post.provider_job_id = post.provider_request_id = None
            post.schedule_revision += 1
            await db.execute(delete(Publication).where(Publication.post_id == post.id))
            for version in versions:
                db.add(Publication(post_id=post.id, platform=version.platform, status="scheduled"))
            await db.commit()
            try:
                publish_post.delay(post.id)
            except CeleryError as exc:
                post.status = "draft"
                post.publish_mode = None
                post.scheduled_at_utc = None
                post.schedule_timezone = None
                await db.execute(delete(Publication).where(Publication.post_id == post.id))
                await db.commit()
                await publish_post_event(user.id, post.id)
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "queue_unavailable",
                        "message": "The schedule was not recreated; your post is safely back in drafts",
                    },
                ) from exc
    except ProviderError as exc:
        if exc.status_code in {400, 404, 409}:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "schedule_already_started",
                    "message": "This post has already started publishing",
                },
            ) from exc
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    await db.commit()
    await publish_post_event(user.id, post.id)
    return post_out(await owned_post(db, user, post.id))


@router.delete("/{post_id}/schedule", response_model=PostOut)
async def cancel_schedule(
    post_id: str, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)
):
    post = await owned_post(db, user, post_id, lock=True)
    if post.status != "scheduled":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "schedule_already_started",
                "message": "This post has already started publishing",
            },
        )
    if not post.provider_job_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "schedule_submission_pending",
                "message": "The schedule is still being registered. Try again in a moment",
            },
        )
    try:
        await UploadPostClient().cancel_schedule(post.provider_job_id)
    except ProviderError as exc:
        raise HTTPException(
            status_code=409 if exc.status_code in {400, 404} else exc.status_code,
            detail={
                "code": "schedule_already_started" if exc.status_code in {400, 404} else exc.code,
                "message": str(exc),
            },
        ) from exc
    post.status = "draft"
    post.publish_mode = None
    post.scheduled_at_utc = None
    post.schedule_timezone = None
    post.provider_job_id = None
    post.provider_request_id = None
    post.media.delete_after = datetime.now(UTC) + timedelta(days=7)
    await db.execute(delete(Publication).where(Publication.post_id == post.id))
    await db.commit()
    await publish_post_event(user.id, post.id)
    return post_out(await owned_post(db, user, post.id))


@router.post("/{post_id}/publications/{platform}/retry", response_model=PostOut, status_code=202)
async def retry_publication(
    post_id: str,
    platform: str,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    post = await owned_post(db, user, post_id, lock=True)
    publication = next((item for item in post.publications if item.platform == platform), None)
    if not publication or publication.status not in {"failed", "action_required"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "publication_not_retryable", "message": "This destination does not need a retry"},
        )
    publication.status = "queued"
    publication.error_code = publication.error_message = None
    post.status = "publishing"
    post.schedule_revision += 1
    await db.commit()
    try:
        retry_platform.delay(post.id, platform)
    except CeleryError as exc:
        publication.status = "failed"
        publication.error_code = "queue_unavailable"
        publication.error_message = "Publishing queue is unavailable"
        post.status = derive_post_status(post)
        await queue_terminal_notification(db, post)
        await db.commit()
        await publish_post_event(user.id, post.id)
        raise HTTPException(
            status_code=503,
            detail={"code": "queue_unavailable", "message": "Retry later; completed posts were not changed"},
        ) from exc
    await publish_post_event(user.id, post.id)
    return post_out(await owned_post(db, user, post.id))


async def remove_post(db: AsyncSession, user: User, post_id: str) -> None:
    post = await owned_post(db, user, post_id, lock=True)
    attempts = max((item.attempts for item in post.publications), default=0)
    has_provider_reference = bool(post.provider_request_id or post.provider_job_id)
    if post.status == "publishing" or (
        post.status == "queued" and (attempts > 0 or has_provider_reference)
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "post_publishing",
                "message": "This post has started publishing and cannot be deleted yet",
            },
        )
    if post.status == "scheduled":
        if post.provider_job_id:
            try:
                await UploadPostClient().cancel_schedule(post.provider_job_id)
            except ProviderError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "schedule_already_started", "message": str(exc)},
                ) from exc
        elif attempts > 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "schedule_submission_pending",
                    "message": "This schedule is still being registered and cannot be deleted yet",
                },
            )

    media = post.media
    await db.delete(post)
    await db.flush()
    remaining_uses = await db.scalar(select(func.count(Post.id)).where(Post.media_id == media.id))
    if not remaining_uses:
        media.delete_after = datetime.now(UTC) + timedelta(hours=24)
    await db.commit()
    await publish_post_event(user.id, post_id, "post.deleted")


@router.post("/bulk-delete", response_model=BulkDeleteOut)
async def bulk_delete_posts(
    payload: BulkDeleteIn,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    deleted_ids: list[str] = []
    failures: list[BulkDeleteFailureOut] = []
    for post_id in sorted(payload.post_ids):
        try:
            await remove_post(db, user, post_id)
            deleted_ids.append(post_id)
        except HTTPException as exc:
            await db.rollback()
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            failures.append(
                BulkDeleteFailureOut(
                    post_id=post_id,
                    code=str(detail.get("code", "delete_failed")),
                    message=str(detail.get("message", "Post could not be deleted")),
                )
            )
    return BulkDeleteOut(deleted_ids=deleted_ids, failures=failures)


@router.delete("/{post_id}", response_model=MessageOut)
async def delete_post(
    post_id: str, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)
):
    await remove_post(db, user, post_id)
    return MessageOut(message="Post deleted")
