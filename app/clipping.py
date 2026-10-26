"""Private clipping access, durable reservations and one ordinary draft per clip."""
import hmac
import math
from datetime import UTC, datetime, timedelta
from typing import Literal

from celery.exceptions import CeleryError
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .admin import require_admin
from .config import get_settings
from .database import get_db
from .deps import get_current_user, require_verified_user
from .entitlements import require_capability
from .events import publish_realtime_event
from .media_routes import media_out
from .models import (
    ClippingAccess,
    ClippingProject,
    EntitlementAuditEvent,
    FeatureFlag,
    GeneratedClip,
    MediaAsset,
    Post,
    User,
    utcnow,
)
from .posts import owned_post, post_out
from .vizard import VizardClient, VizardError

router = APIRouter(tags=["clips"])
PROJECT_OPTIONS = (selectinload(ClippingProject.source), selectinload(ClippingProject.clips).selectinload(GeneratedClip.media), selectinload(ClippingProject.clips).selectinload(GeneratedClip.post))
ACTIVE = ("queued", "submitting", "uncertain", "processing", "importing")


def fail(code: str, message: str, status=409):
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def period_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def configured() -> bool:
    settings = get_settings()
    return bool(settings.vizard_api_key and settings.storage_backend == "r2")


async def require_clipping(db: AsyncSession, user: User) -> ClippingAccess:
    settings = get_settings()
    flag = await db.get(FeatureFlag, "clipping")
    grant = await db.scalar(select(ClippingAccess).where(ClippingAccess.user_id == user.id).execution_options(populate_existing=True))
    if not user.email_verified or not settings.clipping_enabled or not configured() or not flag or not flag.enabled or not grant or not grant.enabled:
        raise fail("clipping_unavailable", "Clips is in private beta", 403)
    return grant


def dispatch_project(project_id: str, *, countdown: int = 0) -> None:
    from .tasks import run_clipping_project
    run_clipping_project.apply_async(args=[project_id], countdown=countdown, queue="clipping")


async def used_seconds(db: AsyncSession, user_id: str) -> int:
    return int(await db.scalar(select(func.coalesce(func.sum(ClippingProject.source_seconds), 0)).where(
        ClippingProject.user_id == user_id, ClippingProject.period == period_now(), ClippingProject.billing_status != "released")) or 0)


async def used_credits(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.coalesce(func.sum(ClippingProject.reserved_credit_milli), 0)).where(
        ClippingProject.period == period_now(), ClippingProject.billing_status != "released")) or 0)


@router.get("/v1/clips/access")
async def access(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    try:
        grant = await require_clipping(db, user)
    except HTTPException:
        return {"enabled": False}
    styles = [{"id": "clean", "name": "Clean"}]
    if settings.vizard_bold_template_id:
        styles.append({"id": "bold", "name": "Bold captions"})
    return {"enabled": True, "monthly_minutes": grant.monthly_minutes, "used_minutes": math.ceil(await used_seconds(db, user.id) / 60),
            "max_source_bytes": settings.clipping_max_upload_bytes, "max_source_seconds": settings.clipping_max_duration_seconds,
            "styles": styles, "model_multiplier": 1.25 if settings.vizard_clip_model == "clip_v2" else 1}


def project_out(project: ClippingProject) -> dict:
    return {"id": project.id, "name": project.name, "status": project.status, "options": project.options,
            "media": media_out(project.source), "error_message": project.error_message, "created_at": project.created_at,
            "clips": [{"id": clip.id, "title": clip.title, "status": clip.status, "media": media_out(clip.media) if clip.media else None,
                       "post_id": clip.post_id, "post_status": clip.post.status if clip.post else None,
                       "error_message": clip.error_message} for clip in project.clips if not clip.deleted_at]}


async def owned_project(db: AsyncSession, user_id: str, project_id: str) -> ClippingProject:
    project = await db.scalar(select(ClippingProject).where(ClippingProject.id == project_id, ClippingProject.user_id == user_id).options(*PROJECT_OPTIONS))
    if not project:
        raise fail("clipping_project_not_found", "Clipping project not found", 404)
    return project


@router.get("/v1/clips/projects")
async def projects(user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    await require_clipping(db, user)
    records = list(await db.scalars(select(ClippingProject).where(ClippingProject.user_id == user.id).options(*PROJECT_OPTIONS).order_by(ClippingProject.created_at.desc()).limit(50)))
    uploads = list(await db.scalars(select(MediaAsset).where(MediaAsset.user_id == user.id, MediaAsset.purpose == "clipping",
        MediaAsset.status.not_in(["failed", "expired"]), ~select(ClippingProject.id).where(ClippingProject.source_media_id == MediaAsset.id).exists()).order_by(MediaAsset.created_at.desc()).limit(10)))
    return {"items": [project_out(project) for project in records], "uploads": [media_out(asset) for asset in uploads]}


@router.get("/v1/clips/projects/{project_id}")
async def project_detail(project_id: str, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    await require_clipping(db, user)
    return project_out(await owned_project(db, user.id, project_id))


class ProjectIn(BaseModel):
    media_id: str
    length: Literal["short", "medium"] = "short"
    style: Literal["clean", "bold"] = "clean"
    keywords: str = Field(default="", max_length=1000)
    subtitles: bool = True
    remove_silence: bool = False


@router.post("/v1/clips/projects", status_code=202)
async def create_project(payload: ProjectIn, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    await require_clipping(db, user)
    settings = get_settings()
    # A single persisted global flag locks global and per-user reservation decisions.
    # Every creator request takes this same lock before inspecting budget/active jobs.
    flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "clipping").with_for_update().execution_options(populate_existing=True))
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    grant = await require_clipping(db, user)
    if not flag or not flag.enabled:
        raise fail("clipping_unavailable", "Clipping is temporarily paused", 403)
    media = await db.scalar(select(MediaAsset).where(MediaAsset.id == payload.media_id, MediaAsset.user_id == user.id))
    if not media:
        raise fail("media_not_found", "Upload not found", 404)
    existing = await db.scalar(select(ClippingProject.id).where(ClippingProject.source_media_id == media.id))
    if existing:
        return project_out(await owned_project(db, user.id, existing))
    if media.purpose != "clipping" or media.storage_backend != "r2" or media.status != "ready" or not media.object_key:
        raise fail("clipping_source_not_ready", "Finish uploading a long video first")
    if not media.duration_seconds or not 1 <= media.duration_seconds <= settings.clipping_max_duration_seconds or media.size_bytes > settings.clipping_max_upload_bytes:
        raise fail("clipping_source_invalid", "Choose a video within the clipping limits", 422)
    if payload.style == "bold" and not settings.vizard_bold_template_id:
        raise fail("clipping_style_unavailable", "Choose an available clip style", 422)
    if await db.scalar(select(ClippingProject.id).where(ClippingProject.user_id == user.id, ClippingProject.status.in_(ACTIVE)).limit(1)):
        raise fail("clipping_job_active", "Let your current video finish before starting another")
    if await used_seconds(db, user.id) + media.duration_seconds > grant.monthly_minutes * 60:
        raise fail("clipping_minutes_exhausted", "You have reached your clipping allowance for this month")
    credits = math.ceil(media.duration_seconds / 60) * (1250 if settings.vizard_clip_model == "clip_v2" else 1000)
    if await used_credits(db) + credits > settings.clipping_monthly_credits * 1000:
        raise fail("clipping_capacity_reached", "Clipping has reached this month's beta capacity")
    project = ClippingProject(user_id=user.id, source_media_id=media.id, name=media.original_name,
                              options=payload.model_dump(exclude={"media_id"}), model=settings.vizard_clip_model,
                              period=period_now(), source_seconds=media.duration_seconds,
                              reserved_credit_milli=credits, next_check_at=utcnow())
    media.delete_after = utcnow() + timedelta(days=7)
    db.add(project)
    await db.commit()
    try:
        dispatch_project(project.id)
    except CeleryError:
        # The periodic reconciler dispatches the durable queued record; no lost work.
        pass
    await publish_realtime_event(user.id, "clips.updated")
    return project_out(await owned_project(db, user.id, project.id))


@router.post("/v1/clips/projects/{project_id}/retry", status_code=202)
async def retry_import(project_id: str, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    await require_clipping(db, user)
    project = await owned_project(db, user.id, project_id)
    if not project.provider_project_id:
        raise fail("clipping_submission_review", "This submission needs owner review before it can be retried")
    if project.status not in {"ready", "importing", "processing", "failed"}:
        raise fail("clipping_not_retryable", "This project cannot be retried yet")
    project.status, project.next_check_at, project.error_message = "processing", utcnow(), None
    await db.commit()
    try:
        dispatch_project(project.id)
    except CeleryError:
        pass
    return {"message": "Existing clips will be checked without submitting your video again"}


@router.post("/v1/clips/items/{clip_id}/draft")
async def clip_draft(clip_id: str, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    await require_clipping(db, user)
    await require_capability(db, user, "publish")
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    clip = await db.scalar(select(GeneratedClip).where(GeneratedClip.id == clip_id, GeneratedClip.user_id == user.id, GeneratedClip.deleted_at.is_(None)).with_for_update())
    if not clip:
        raise fail("clip_not_found", "Clip not found", 404)
    if clip.post_id:
        return post_out(await owned_post(db, user, clip.post_id))
    media = await db.get(MediaAsset, clip.media_id) if clip.media_id else None
    if clip.status != "ready" or not media or media.status != "ready":
        raise fail("clip_not_ready", "This clip is not ready to publish")
    post = Post(user_id=user.id, media_id=media.id, title=clip.title, caption="", hashtags=[], content_format_version=2)
    db.add(post)
    await db.flush()
    clip.post_id = post.id
    media.delete_after = None
    await db.commit()
    await publish_realtime_event(user.id, "clips.updated")
    return post_out(await owned_post(db, user, post.id))


@router.delete("/v1/clips/items/{clip_id}")
async def delete_clip(clip_id: str, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    await require_clipping(db, user)
    clip = await db.scalar(select(GeneratedClip).where(GeneratedClip.id == clip_id, GeneratedClip.user_id == user.id).with_for_update())
    if not clip:
        raise fail("clip_not_found", "Clip not found", 404)
    if clip.post_id:
        raise fail("clip_in_use", "Delete the associated post before deleting its clip")
    clip.deleted_at = utcnow()
    if clip.media_id:
        media = await db.get(MediaAsset, clip.media_id)
        if media:
            media.delete_after = utcnow()
    await db.commit()
    await publish_realtime_event(user.id, "clips.updated")
    return {"message": "Clip deleted"}


class FeatureIn(BaseModel):
    enabled: bool
    reason: str = Field(min_length=5, max_length=1000)


class GrantIn(FeatureIn):
    monthly_minutes: int = Field(default=60, ge=1, le=10000)


class ReconcileIn(BaseModel):
    provider_project_id: str = Field(pattern=r"^\d{1,64}$")
    reason: str = Field(min_length=5, max_length=1000)


@router.get("/v1/admin/clipping")
async def admin_status(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    del admin
    flag = await db.get(FeatureFlag, "clipping")
    rows = list(await db.scalars(select(ClippingProject).order_by(ClippingProject.created_at.desc()).limit(30)))
    return {"configured": configured(), "environment_enabled": get_settings().clipping_enabled, "enabled": bool(flag and flag.enabled),
            "used_credits": math.ceil(await used_credits(db) / 1000), "budget_credits": get_settings().clipping_monthly_credits,
            "projects": [{"id": row.id, "name": row.name, "status": row.status, "error_message": row.error_message} for row in rows]}


@router.patch("/v1/admin/clipping")
async def set_enabled(payload: FeatureIn, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    flag = await db.get(FeatureFlag, "clipping")
    if not flag:
        flag = FeatureFlag(key="clipping")
        db.add(flag)
    flag.enabled, flag.updated_by_user_id = payload.enabled, admin.id
    db.add(EntitlementAuditEvent(user_id=admin.id, actor_user_id=admin.id, action="clipping_feature_updated", reason=payload.reason, details={"enabled": payload.enabled}))
    await db.commit()
    return {"enabled": flag.enabled}


@router.put("/v1/admin/clipping/users/{user_id}")
async def set_grant(user_id: str, payload: GrantIn, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user:
        raise fail("user_not_found", "Creator not found", 404)
    grant = await db.get(ClippingAccess, user_id)
    if not grant:
        grant = ClippingAccess(user_id=user_id)
        db.add(grant)
    grant.enabled, grant.monthly_minutes = payload.enabled, payload.monthly_minutes
    db.add(EntitlementAuditEvent(user_id=user_id, actor_user_id=admin.id, action="clipping_grant_updated", reason=payload.reason,
                                details={"enabled": payload.enabled, "monthly_minutes": payload.monthly_minutes}))
    await db.commit()
    await publish_realtime_event(user_id, "clips.updated")
    return {"enabled": grant.enabled, "monthly_minutes": grant.monthly_minutes}


@router.post("/v1/admin/clipping/projects/{project_id}/reconcile")
async def reconcile(project_id: str, payload: ReconcileIn, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    project = await db.get(ClippingProject, project_id)
    if not project or project.status != "uncertain":
        raise fail("clipping_review_unavailable", "Only uncertain submissions need this review")
    try:
        result = await VizardClient().status(payload.provider_project_id)
    except VizardError as exc:
        raise fail("clipping_provider_unavailable", str(exc), 503) from exc
    if result.get("projectName") != f"Reverb:{project.id}":
        raise fail("clipping_project_mismatch", "The Vizard project name does not match this submission")
    project = await db.scalar(select(ClippingProject).where(ClippingProject.id == project_id).with_for_update().execution_options(populate_existing=True))
    if not project or project.status != "uncertain":
        raise fail("clipping_review_unavailable", "The submission has already been reviewed")
    occupied = await db.scalar(select(ClippingProject.id).where(ClippingProject.provider_project_id == payload.provider_project_id, ClippingProject.id != project_id))
    if occupied:
        raise fail("clipping_project_mismatch", "That Vizard project is already assigned")
    project.provider_project_id, project.status, project.next_check_at, project.error_message = payload.provider_project_id, "processing", utcnow(), None
    db.add(EntitlementAuditEvent(user_id=project.user_id, actor_user_id=admin.id, action="clipping_submission_reconciled", reason=payload.reason, details={"project_id": project.id}))
    await db.commit()
    dispatch_project(project.id)
    return {"message": "Existing submission matched and scheduled for reconciliation"}


@router.post("/v1/webhooks/vizard", status_code=202)
async def callback(request: Request, token: str = Query(default=""), db: AsyncSession = Depends(get_db)):
    secret = get_settings().vizard_webhook_token
    if not secret or not hmac.compare_digest(token, secret):
        raise fail("invalid_callback", "Invalid callback", 401)
    body = await request.body()
    if len(body) > 65536:
        raise fail("callback_too_large", "Invalid callback", 413)
    import json
    try:
        payload = json.loads(body)
        provider_id = str(payload.get("projectId") or "")
    except (ValueError, AttributeError) as exc:
        raise fail("invalid_callback", "Invalid callback", 400) from exc
    project = await db.scalar(select(ClippingProject).where(ClippingProject.provider_project_id == provider_id))
    if project and project.status in {"processing", "importing"}:
        # A callback is only a wake-up signal. Never trust its result or media URL.
        project.next_check_at = utcnow()
        await db.commit()
    return {"received": True}
