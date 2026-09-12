from __future__ import annotations

from datetime import UTC, datetime, timedelta

from celery.exceptions import CeleryError
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import get_settings
from .database import get_db
from .deps import get_current_user
from .entitlements import require_capability
from .models import AIGenerationJob, AIUsageCounter, FeatureFlag, Post, User

router = APIRouter(prefix="/v1/ai", tags=["ai"])
ADJUSTMENTS = {"shorter", "more_engaging", "professional", "casual", "regenerate"}


class GenerateIn(BaseModel):
    post_id: str
    generation_context: str = Field(default="", max_length=2000)

    @field_validator("generation_context")
    @classmethod
    def clean_generation_context(cls, value: str) -> str:
        return value.strip()


class AdjustIn(BaseModel):
    adjustment: str = Field(min_length=3, max_length=32)


def job_out(job: AIGenerationJob) -> dict:
    return {
        "id": job.id,
        "post_id": job.post_id,
        "media_id": job.media_id,
        "parent_job_id": job.parent_job_id,
        "kind": job.kind,
        "adjustment": job.adjustment,
        "generation_context": job.generation_context or "",
        "status": job.status,
        "model": job.model,
        "candidate": job.candidate,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
    }


async def ensure_ai_available(db: AsyncSession) -> None:
    settings = get_settings()
    flag = await db.get(FeatureFlag, "ai")
    if not settings.ai_enabled or (flag and not flag.enabled):
        raise HTTPException(status_code=503, detail={"code": "ai_disabled", "message": "AI creation is temporarily paused"})
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail={"code": "ai_not_configured", "message": "AI creation is not configured yet"})


async def consume_usage(db: AsyncSession, user_id: str, kind: str) -> None:
    settings = get_settings()
    usage_date = datetime.now(UTC).date().isoformat()
    personal_limit = settings.ai_video_daily_limit if kind == "video" else settings.ai_adjustment_daily_limit
    global_limit = settings.ai_global_video_daily_limit if kind == "video" else settings.ai_global_adjustment_daily_limit
    await db.scalar(select(User).where(User.id == user_id).with_for_update())
    counter = await db.scalar(
        select(AIUsageCounter)
        .where(AIUsageCounter.user_id == user_id, AIUsageCounter.usage_date == usage_date, AIUsageCounter.kind == kind)
        .with_for_update()
    )
    total = await db.scalar(
        select(func.coalesce(func.sum(AIUsageCounter.count), 0)).where(
            AIUsageCounter.usage_date == usage_date, AIUsageCounter.kind == kind
        )
    )
    if counter and counter.count >= personal_limit:
        reset_at = datetime.combine(datetime.now(UTC).date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        raise HTTPException(status_code=429, detail={"code": "ai_fair_use_limit", "message": "You’ve reached today’s fair-use limit", "reset_at": reset_at.isoformat()})
    if int(total or 0) >= global_limit:
        raise HTTPException(status_code=503, detail={"code": "ai_daily_capacity_reached", "message": "AI creation has reached today’s capacity. Try again tomorrow"})
    if not counter:
        counter = AIUsageCounter(user_id=user_id, usage_date=usage_date, kind=kind, count=0)
        db.add(counter)
    counter.count += 1
    await db.flush()


async def enqueue(job: AIGenerationJob) -> None:
    try:
        from .tasks import run_ai_generation
        run_ai_generation.delay(job.id)
    except CeleryError as exc:
        raise HTTPException(status_code=503, detail={"code": "ai_queue_unavailable", "message": "AI creation is temporarily unavailable"}) from exc


@router.post("/generations", status_code=202)
async def create_generation(payload: GenerateIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await require_capability(db, user, "ai_creation")
    await ensure_ai_available(db)
    post = await db.scalar(
        select(Post).where(Post.id == payload.post_id, Post.user_id == user.id).options(selectinload(Post.media), selectinload(Post.versions))
    )
    if not post:
        raise HTTPException(status_code=404, detail={"code": "post_not_found", "message": "Draft not found"})
    if post.media.status != "ready":
        raise HTTPException(status_code=409, detail={"code": "media_not_ready", "message": "Finish processing the video first"})
    if not post.versions:
        raise HTTPException(status_code=422, detail={"code": "platform_required", "message": "Choose at least one platform first"})
    # Serialize generation reservations for this creator. Without this lock,
    # two browser tabs could both observe no active job and queue two uploads.
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    active = await db.scalar(
        select(AIGenerationJob.id).where(
            AIGenerationJob.user_id == user.id,
            AIGenerationJob.kind == "video",
            AIGenerationJob.status.in_(["queued", "processing"]),
            # Preserve the one-at-a-time reservation for other drafts, but do
            # not let a stale job for this draft block analysis of its new
            # video after replacement.
            or_(AIGenerationJob.post_id != post.id, AIGenerationJob.media_id == post.media_id),
        )
    )
    if active:
        raise HTTPException(status_code=409, detail={"code": "ai_generation_in_progress", "message": "One video generation is already running"})
    await consume_usage(db, user.id, "video")
    job = AIGenerationJob(
        user_id=user.id,
        post_id=post.id,
        media_id=post.media_id,
        kind="video",
        generation_context=payload.generation_context,
        status="queued",
        model=get_settings().gemini_model,
    )
    db.add(job)
    await db.commit()
    try:
        await enqueue(job)
    except HTTPException:
        job.status = "failed"
        job.error_code = "ai_queue_unavailable"
        await db.commit()
        raise
    return job_out(job)


@router.get("/generations/latest")
async def get_latest_generation(
    post_id: str = Query(min_length=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_capability(db, user, "ai_creation")
    current_media_id = await db.scalar(
        select(Post.media_id).where(Post.id == post_id, Post.user_id == user.id)
    )
    if not current_media_id:
        raise HTTPException(status_code=404, detail={"code": "post_not_found", "message": "Draft not found"})
    job = await db.scalar(
        select(AIGenerationJob)
        .where(
            AIGenerationJob.post_id == post_id,
            AIGenerationJob.user_id == user.id,
            AIGenerationJob.media_id == current_media_id,
        )
        .order_by(AIGenerationJob.created_at.desc(), AIGenerationJob.id.desc())
    )
    return job_out(job) if job else None


@router.get("/generations/{job_id}")
async def get_generation(job_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await require_capability(db, user, "ai_creation")
    job = await db.scalar(select(AIGenerationJob).where(AIGenerationJob.id == job_id, AIGenerationJob.user_id == user.id))
    if not job:
        raise HTTPException(status_code=404, detail={"code": "ai_job_not_found", "message": "AI generation not found"})
    return job_out(job)


@router.post("/generations/{job_id}/adjust", status_code=202)
async def adjust_generation(job_id: str, payload: AdjustIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await require_capability(db, user, "ai_creation")
    await ensure_ai_available(db)
    adjustment = payload.adjustment.casefold().replace(" ", "_")
    if adjustment not in ADJUSTMENTS:
        raise HTTPException(status_code=422, detail={"code": "invalid_adjustment", "message": "Choose a supported adjustment"})
    parent = await db.scalar(select(AIGenerationJob).where(AIGenerationJob.id == job_id, AIGenerationJob.user_id == user.id))
    if not parent or parent.status != "completed" or not parent.candidate:
        raise HTTPException(status_code=409, detail={"code": "ai_candidate_not_ready", "message": "Wait for the current suggestion to finish"})
    current_media_id = await db.scalar(
        select(Post.media_id).where(Post.id == parent.post_id, Post.user_id == user.id)
    )
    if not current_media_id:
        raise HTTPException(status_code=404, detail={"code": "post_not_found", "message": "Draft not found"})
    if parent.media_id != current_media_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ai_candidate_stale",
                "message": "The video changed. Generate a new suggestion for the current video first",
            },
        )
    await consume_usage(db, user.id, "adjustment")
    job = AIGenerationJob(
        user_id=user.id,
        post_id=parent.post_id,
        media_id=parent.media_id,
        parent_job_id=parent.id,
        kind="adjustment",
        adjustment=adjustment,
        generation_context=parent.generation_context or "",
        status="queued",
        model=get_settings().gemini_model,
    )
    db.add(job)
    await db.commit()
    try:
        await enqueue(job)
    except HTTPException:
        job.status = "failed"
        job.error_code = "ai_queue_unavailable"
        await db.commit()
        raise
    return job_out(job)
