from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from celery.exceptions import CeleryError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db
from .deps import get_current_user, require_verified_user
from .entitlements import require_capability
from .events import publish_realtime_event
from .media_routes import owned_media
from .models import MediaAsset, SubtitleJob, User, new_id
from .security import create_media_token, decode_media_token
from .storage import R2Storage, StorageError
from .subtitles import clean_word

router = APIRouter(prefix="/v1", tags=["subtitles"])
ACTIVE = {"queued", "transcribing", "rendering"}


class SubtitleIn(BaseModel):
    base_job_id: str | None = None
    texts: list[str] | None = Field(default=None, min_length=1, max_length=4000)
    retry: bool = False

    @model_validator(mode="after")
    def validate_edits(self):
        if (self.texts is None) != (self.base_job_id is None):
            raise ValueError("Corrections need the original subtitle version")
        if self.texts is not None:
            self.texts = [clean_word(text) for text in self.texts]
            if any(not text or len(text) > 80 for text in self.texts):
                raise ValueError("Each subtitle word must contain 1–80 characters")
        return self


def subtitle_out(job: SubtitleJob) -> dict:
    settings = get_settings()
    preview = None
    if job.status == "ready":
        token = create_media_token(f"subtitle:{job.id}")
        preview = f"{settings.api_public_url}/v1/subtitles/{job.id}/video?token={token}"
    return {
        "id": job.id,
        "media_id": job.media_id,
        "status": job.status,
        "words": job.words,
        "preview_url": preview,
        "error_message": job.error_message,
        "width": job.width,
        "height": job.height,
        "size_bytes": job.size_bytes,
    }


async def owned_subtitle(db: AsyncSession, user: User, job_id: str) -> SubtitleJob:
    job = await db.scalar(select(SubtitleJob).where(SubtitleJob.id == job_id, SubtitleJob.user_id == user.id))
    if not job:
        raise HTTPException(404, detail={"code": "subtitles_not_found", "message": "Subtitles not found"})
    return job


async def validate_subtitle_selection(db: AsyncSession, user: User, media_id: str, job_id: str | None):
    if not job_id:
        return
    job = await owned_subtitle(db, user, job_id)
    if job.media_id != media_id:
        raise HTTPException(
            422,
            detail={"code": "subtitle_media_mismatch", "message": "These subtitles belong to another video"},
        )


@router.post("/media/{media_id}/subtitles", status_code=202)
async def start_subtitles(
    media_id: str,
    payload: SubtitleIn,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    await require_capability(db, user, "publish")
    # Serializes job creation and retries for this user's media and rate budget.
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    asset = await owned_media(db, user, media_id, lock=True)
    if asset.purpose == "clipping" or asset.baked_subtitles:
        raise HTTPException(409, detail={"code": "subtitles_unavailable", "message": "Choose a generated clip without existing subtitles to add Auto subtitles"})
    if asset.status != "ready":
        raise HTTPException(
            409, detail={"code": "media_not_ready", "message": "Finish uploading your video first"}
        )
    words = []
    fingerprint = "auto-v1"
    if payload.texts is not None:
        base = await owned_subtitle(db, user, payload.base_job_id)
        if base.media_id != media_id or not base.words or len(base.words) != len(payload.texts):
            raise HTTPException(
                422,
                detail={
                    "code": "invalid_subtitle_edit",
                    "message": "Correct the existing words, then try again",
                },
            )
        words = [{**word, "text": text} for word, text in zip(base.words, payload.texts, strict=True)]
        if words == base.words and base.status == "ready":
            return subtitle_out(base)
        fingerprint = hashlib.sha256(json.dumps(words, sort_keys=True).encode()).hexdigest()
    job = await db.scalar(
        select(SubtitleJob).where(SubtitleJob.media_id == media_id, SubtitleJob.fingerprint == fingerprint)
    )
    if job and (job.status != "failed" or not payload.retry):
        return subtitle_out(job)
    settings = get_settings()
    if not settings.subtitles_enabled or (
        not words and not (job and job.words) and not settings.groq_api_key
    ):
        raise HTTPException(
            503,
            detail={"code": "subtitles_unavailable", "message": "Auto subtitles are temporarily unavailable"},
        )
    active = await db.scalar(
        select(func.count())
        .select_from(SubtitleJob)
        .where(SubtitleJob.user_id == user.id, SubtitleJob.status.in_(ACTIVE))
    )
    recent = await db.scalar(
        select(func.coalesce(func.sum(SubtitleJob.attempts), 0)).where(
            SubtitleJob.user_id == user.id, SubtitleJob.updated_at >= datetime.now(UTC) - timedelta(days=1)
        )
    )
    if active >= 2 or recent >= settings.subtitles_daily_limit:
        raise HTTPException(
            429,
            detail={
                "code": "subtitle_limit",
                "message": "Subtitle processing limit reached. Please try again later",
            },
        )
    if job:
        job.status, job.error_message, job.run_token = "queued", None, new_id()
        job.attempts += 1
    else:
        job = SubtitleJob(user_id=user.id, media_id=media_id, fingerprint=fingerprint, words=words)
        db.add(job)
    asset.delete_after = datetime.now(UTC) + timedelta(days=7) if asset.delete_after else None
    await db.commit()
    try:
        from .tasks import process_subtitles

        process_subtitles.delay(job.id, job.run_token)
    except CeleryError as exc:
        job.status, job.error_message = "failed", "Subtitles couldn’t start. Please retry."
        await db.commit()
        raise HTTPException(
            503, detail={"code": "subtitle_queue_unavailable", "message": job.error_message}
        ) from exc
    await publish_realtime_event(user.id, "subtitles.updated", subtitle_job_id=job.id, media_id=media_id)
    return subtitle_out(job)


@router.get("/subtitles/{job_id}")
async def get_subtitles(
    job_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return subtitle_out(await owned_subtitle(db, user, job_id))


@router.get("/subtitles/{job_id}/video", include_in_schema=False)
async def subtitle_video(job_id: str, token: str = Query(...), db: AsyncSession = Depends(get_db)):
    try:
        if decode_media_token(token) != f"subtitle:{job_id}":
            raise jwt.InvalidTokenError()
    except jwt.InvalidTokenError as exc:
        raise HTTPException(401, detail="Invalid subtitle video token") from exc
    job = await db.get(SubtitleJob, job_id)
    if not job or job.status != "ready":
        raise HTTPException(404, detail="Subtitle video not found")
    asset = await db.get(MediaAsset, job.media_id)
    if asset.storage_backend == "r2" and job.object_key:
        try:
            return RedirectResponse(await R2Storage().presign_download(job.object_key))
        except StorageError as exc:
            raise HTTPException(503, detail="Video temporarily unavailable") from exc
    if not job.storage_path or not Path(job.storage_path).is_file():
        raise HTTPException(404, detail="Subtitle video not found")
    return FileResponse(job.storage_path, media_type="video/mp4")
