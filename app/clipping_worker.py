"""Dedicated clipping work: submit once, reconcile repeatedly, import idempotently."""
import asyncio
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from celery.exceptions import CeleryError
from kombu.exceptions import OperationalError as BrokerError
from sqlalchemy import func, select

from .config import get_settings
from .database import TaskSessionLocal
from .events import publish_realtime_event
from .media import create_thumbnail, probe_video
from .models import ClippingAccess, ClippingProject, FeatureFlag, GeneratedClip, MediaAsset, utcnow
from .storage import R2Storage, StorageError
from .vizard import VizardClient, VizardError


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def claim(project_id: str):
    async with TaskSessionLocal() as db:
        flag = await db.scalar(select(FeatureFlag).where(FeatureFlag.key == "clipping").with_for_update())
        project = await db.scalar(select(ClippingProject).where(ClippingProject.id == project_id).with_for_update())
        if not project or project.status not in {"queued", "submitting", "processing", "importing"}:
            return None
        now = utcnow()
        if project.lease_until and aware(project.lease_until) > now:
            return None
        if project.status == "submitting" and not project.provider_project_id:
            project.status = "uncertain"
            project.error_message = "The submission needs an owner review before retrying. Your video will not be submitted twice."
            project.next_check_at = None
            await db.commit()
            await publish_realtime_event(project.user_id, "clips.updated")
            return None
        if not project.provider_project_id:
            grant = await db.get(ClippingAccess, project.user_id)
            settings = get_settings()
            if not settings.clipping_enabled or not flag or not flag.enabled or not grant or not grant.enabled:
                project.next_check_at = now + timedelta(minutes=1)
                await db.commit()
                return None
            minute_count = await db.scalar(select(func.count(ClippingProject.id)).where(ClippingProject.submission_started_at >= now - timedelta(minutes=1)))
            hour_count = await db.scalar(select(func.count(ClippingProject.id)).where(ClippingProject.submission_started_at >= now - timedelta(hours=1)))
            if minute_count >= 3 or hour_count >= 20:
                project.next_check_at = now + timedelta(minutes=1)
                await db.commit()
                return None
            project.status, project.submission_started_at, project.billing_status = "submitting", now, "submitted"
        project.run_token = str(uuid.uuid4())
        project.lease_until = now + timedelta(minutes=15)
        project.next_check_at = project.lease_until
        await db.commit()
        return project


async def finish(project_id: str, run_token: str, *, status: str, message: str | None = None,
                 provider_id: str | None = None, release=False, delay: int | None = None) -> None:
    async with TaskSessionLocal() as db:
        project = await db.scalar(select(ClippingProject).where(ClippingProject.id == project_id).with_for_update())
        if not project or project.run_token != run_token:
            return
        project.status, project.error_message = status, message
        if provider_id:
            project.provider_project_id = provider_id
        if release:
            project.billing_status = "released"
        project.lease_until, project.run_token = None, None
        project.next_check_at = utcnow() + timedelta(seconds=delay) if delay is not None else None
        source = await db.get(MediaAsset, project.source_media_id)
        if source:
            source.delete_after = utcnow() + timedelta(days=7)
        await db.commit()
        await publish_realtime_event(project.user_id, "clips.updated")


async def import_clip(project: ClippingProject, item: dict) -> bool:
    provider_id = str(item.get("videoId") or "")
    if not provider_id.isdigit() or len(provider_id) > 64:
        raise VizardError("A generated clip had invalid metadata")
    async with TaskSessionLocal() as db:
        current = await db.scalar(select(ClippingProject).where(ClippingProject.id == project.id).with_for_update())
        if not current or current.run_token != project.run_token:
            return False
        clip = await db.scalar(select(GeneratedClip).where(GeneratedClip.project_id == project.id, GeneratedClip.provider_clip_id == provider_id))
        if clip and (clip.deleted_at or clip.status == "ready"):
            return True
        if not clip:
            clip = GeneratedClip(user_id=project.user_id, project_id=project.id, provider_clip_id=provider_id,
                                 title=str(item.get("title") or "Untitled clip")[:255])
            db.add(clip)
            await db.flush()
        if not clip.media_id:
            asset = MediaAsset(user_id=project.user_id, original_name=f"clip-{clip.id}.mp4", mime_type="video/mp4",
                               size_bytes=0, uploaded_bytes=0, storage_path="", storage_backend="r2", status="processing",
                               baked_subtitles=bool(project.options.get("subtitles", True)), delete_after=utcnow() + timedelta(days=30))
            db.add(asset)
            await db.flush()
            asset.object_key = R2Storage.object_key(project.user_id, asset.id, ".mp4")
            asset.thumbnail_key = R2Storage.thumbnail_key(project.user_id, asset.id)
            clip.media_id = asset.id
        asset_id, clip_id = clip.media_id, clip.id
        clip.status, clip.error_message = "importing", None
        await db.commit()
    settings = get_settings()
    try:
        with tempfile.TemporaryDirectory(prefix="reverb-clip-") as temporary:
            path = Path(temporary) / "clip.mp4"
            thumbnail = Path(temporary) / "thumbnail.jpg"
            await VizardClient().download(str(item.get("videoUrl") or ""), path, settings.max_upload_bytes)
            duration, width, height = await asyncio.to_thread(probe_video, path)
            if not 1 <= duration <= 180 or width <= 0 or height <= 0:
                raise VizardError("This generated clip does not meet Reverb's video limits")
            await asyncio.to_thread(create_thumbnail, path, thumbnail)
            async with TaskSessionLocal() as db:
                asset = await db.get(MediaAsset, asset_id)
                object_key, thumbnail_key = asset.object_key, asset.thumbnail_key
            storage = R2Storage()
            await storage.put_file(path, object_key, "video/mp4")
            await storage.put_file(thumbnail, thumbnail_key, "image/jpeg")
            async with TaskSessionLocal() as db:
                current = await db.scalar(select(ClippingProject).where(ClippingProject.id == project.id).with_for_update())
                if not current or current.run_token != project.run_token:
                    return False
                clip = await db.get(GeneratedClip, clip_id)
                asset = await db.get(MediaAsset, asset_id)
                asset.duration_seconds, asset.width, asset.height = duration, width, height
                asset.size_bytes = asset.uploaded_bytes = path.stat().st_size
                asset.status, clip.status, clip.error_message = "ready", "ready", None
                current.lease_until = utcnow() + timedelta(minutes=15)
                await db.commit()
                await publish_realtime_event(project.user_id, "clips.updated")
        return True
    except (VizardError, StorageError, RuntimeError, ValueError, KeyError, IndexError, OSError, subprocess.SubprocessError) as exc:
        async with TaskSessionLocal() as db:
            current = await db.scalar(select(ClippingProject).where(ClippingProject.id == project.id).with_for_update())
            if not current or current.run_token != project.run_token:
                return False
            clip = await db.get(GeneratedClip, clip_id)
            asset = await db.get(MediaAsset, asset_id)
            clip.status = "failed"
            clip.error_message = str(exc) if isinstance(exc, VizardError) else "This clip could not be saved. Please retry."
            asset.status = "failed"
            await db.commit()
        return False


async def run_project(project_id: str) -> None:
    project = await claim(project_id)
    if not project:
        return
    token = project.run_token
    if not project.provider_project_id:
        async with TaskSessionLocal() as db:
            source = await db.get(MediaAsset, project.source_media_id)
        if not source or source.status != "ready" or not source.object_key:
            await finish(project.id, token, status="failed", message="The source video is no longer available", release=True)
            return
        try:
            url = await R2Storage().presign_download(source.object_key, ttl_seconds=21600)
        except StorageError:
            await finish(project.id, token, status="failed", message="The source video could not be retrieved", release=True)
            return
        try:
            provider_id = await VizardClient().submit(url, {**project.options, "model": project.model}, f"Reverb:{project.id}",
                                                      "mov" if source.mime_type == "video/quicktime" else "mp4")
        except VizardError as exc:
            await finish(project.id, token, status="uncertain" if exc.uncertain else "failed",
                         message="The submission needs an owner review before retrying. Your video will not be submitted twice." if exc.uncertain else str(exc), release=not exc.uncertain)
            return
        await finish(project.id, token, status="processing", provider_id=provider_id, delay=30)
        return

    try:
        result = await VizardClient().status(project.provider_project_id)
        if result.get("projectId") is not None and str(result["projectId"]) != project.provider_project_id:
            raise VizardError("The returned project did not match this submission")
        if result.get("code") == 1000:
            if aware(project.created_at) < utcnow() - timedelta(hours=24):
                await finish(project.id, token, status="failed", message="Processing is taking longer than expected. Check this project again later.")
            else:
                await finish(project.id, token, status="processing", delay=60)
            return
        videos = result.get("videos")
        if not isinstance(videos, list):
            raise VizardError("Vizard has not returned the clips yet", retryable=True)
        if not videos:
            await finish(project.id, token, status="no_clips", message="No suitable clips matched this video and topic.")
            return
        async with TaskSessionLocal() as db:
            current = await db.get(ClippingProject, project.id)
            if current.run_token != token:
                return
            current.status = "importing"
            await db.commit()
        complete = True
        for item in videos[:5]:
            if not isinstance(item, dict):
                complete = False
                continue
            try:
                complete = await import_clip(project, item) and complete
            except VizardError:
                complete = False
        await finish(project.id, token, status="ready", message=None if complete else "Some clips could not be saved. Retry saving to recover them.")
    except VizardError as exc:
        await finish(project.id, token, status="processing" if exc.retryable else "failed", message=str(exc), delay=120 if exc.retryable else None)


async def reconcile_due() -> None:
    from .clipping import dispatch_project
    now = utcnow()
    async with TaskSessionLocal() as db:
        rows = list(await db.scalars(select(ClippingProject).where(
            ClippingProject.status.in_(["queued", "submitting", "processing", "importing"]),
            ClippingProject.next_check_at <= now).order_by(ClippingProject.next_check_at).limit(20).with_for_update(skip_locked=True)))
        ids = [row.id for row in rows]
        for row in rows:
            row.next_check_at = now + timedelta(minutes=1)
        await db.commit()
    for project_id in ids:
        try:
            dispatch_project(project_id)
        except (CeleryError, BrokerError, OSError):
            continue
