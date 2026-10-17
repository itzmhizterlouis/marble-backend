from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update

from .config import get_settings
from .database import TaskSessionLocal
from .events import publish_realtime_event
from .media import probe_video
from .models import MediaAsset, SubtitleJob
from .storage import R2Storage, materialize_object
from .subtitles import SubtitleError, extract_audio, render_video, transcribe

logger = logging.getLogger(__name__)


async def notify(job):
    await publish_realtime_event(
        job.user_id, "subtitles.updated", subtitle_job_id=job.id, media_id=job.media_id
    )


async def process(job_id: str, run_token: str):
    settings = get_settings()
    async with TaskSessionLocal() as db:
        claimed = await db.execute(
            update(SubtitleJob)
            .where(
                SubtitleJob.id == job_id, SubtitleJob.run_token == run_token, SubtitleJob.status == "queued"
            )
            .values(status="transcribing", updated_at=datetime.now(UTC))
        )
        await db.commit()
        if not claimed.rowcount:
            return
        job = await db.get(SubtitleJob, job_id)
        await notify(job)
        directory = Path(tempfile.mkdtemp(prefix="reverb-subtitles-", dir=settings.storage_root))
        output = directory / "subtitled.mp4"
        retained = False
        persisted = False
        uploaded_key = None
        try:
            asset = await db.get(MediaAsset, job.media_id)
            if not asset or asset.status != "ready":
                raise SubtitleError("This upload is no longer available. Please upload the video again.")
            async with materialize_object(
                storage_backend=asset.storage_backend,
                object_key=asset.object_key,
                storage_path=asset.storage_path,
                filename=asset.original_name,
            ) as source:
                if not job.words:
                    audio = directory / "audio.flac"
                    await asyncio.to_thread(extract_audio, source, audio)
                    job.words = await transcribe(audio, float(asset.duration_seconds or 600))
                    # Persist before rendering so a render retry never re-transcribes.
                job.status = "rendering"
                await db.commit()
                await notify(job)
                await asyncio.to_thread(render_video, source, output, job.words)
            _, width, height = await asyncio.to_thread(probe_video, output)
            size = output.stat().st_size
            if size > settings.max_upload_bytes:
                raise SubtitleError("The subtitled video is too large. Please use a shorter video.")
            if asset.storage_backend == "r2":
                uploaded_key = f"users/{job.user_id}/media/{job.media_id}/subtitles/{job.id}-{run_token}.mp4"
                await R2Storage().put_file(output, uploaded_key, "video/mp4")
            # A cleanup or stale-job timeout must not resurrect a discarded job.
            done = await db.execute(
                update(SubtitleJob)
                .where(
                    SubtitleJob.id == job_id,
                    SubtitleJob.run_token == run_token,
                    SubtitleJob.status == "rendering",
                )
                .values(
                    status="ready",
                    object_key=uploaded_key,
                    storage_path=str(output) if asset.storage_backend == "local" else None,
                    width=width,
                    height=height,
                    size_bytes=size,
                    error_message=None,
                )
            )
            await db.commit()
            persisted = bool(done.rowcount)
            retained = bool(done.rowcount) and asset.storage_backend == "local"
            if not done.rowcount and uploaded_key:
                await R2Storage().delete_object(uploaded_key)
            await db.refresh(job)
            await notify(job)
        except Exception as exc:
            await db.rollback()
            if uploaded_key and not persisted:
                try:
                    await R2Storage().delete_object(uploaded_key)
                except Exception:
                    logger.warning("Could not remove unfinished subtitle output job_id=%s", job_id)
            # Never store/log audio, words, keys or provider response bodies.
            logger.warning("Subtitle job failed job_id=%s error_class=%s", job_id, type(exc).__name__)
            message = (
                str(exc) if isinstance(exc, SubtitleError) else "We couldn’t prepare subtitles. Please retry."
            )
            await db.execute(
                update(SubtitleJob)
                .where(
                    SubtitleJob.id == job_id,
                    SubtitleJob.run_token == run_token,
                    SubtitleJob.status.in_(["transcribing", "rendering"]),
                )
                .values(status="failed", error_message=message)
            )
            await db.commit()
            await db.refresh(job)
            await notify(job)
        finally:
            if retained:
                for path in directory.iterdir():
                    if path != output:
                        path.unlink(missing_ok=True)
            else:
                await asyncio.to_thread(shutil.rmtree, directory, True)


async def reconcile():
    """Recover lost queue deliveries; give interrupted workers a retryable state."""
    from .tasks import process_subtitles

    async with TaskSessionLocal() as db:
        now = datetime.now(UTC)
        jobs = list(
            await db.scalars(
                select(SubtitleJob).where(
                    SubtitleJob.status.in_(["queued", "transcribing", "rendering"]),
                    SubtitleJob.updated_at < now - timedelta(minutes=1),
                )
            )
        )
        for job in jobs:
            updated = job.updated_at.replace(tzinfo=UTC) if job.updated_at.tzinfo is None else job.updated_at
            if now - updated > timedelta(minutes=40):
                job.status, job.error_message = "failed", "Subtitle processing was interrupted. Please retry."
                await db.commit()
                await notify(job)
            elif job.status == "queued":
                process_subtitles.delay(job.id, job.run_token)
