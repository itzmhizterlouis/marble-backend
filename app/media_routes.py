import asyncio
import hashlib
import hmac
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect

from .config import get_settings
from .database import get_db
from .deps import get_current_user, require_verified_user
from .media import append_chunk, create_thumbnail, parse_content_range, probe_video, sha256_file
from .models import MediaAsset, Post, User
from .schemas import MediaInitIn, MediaOut, MessageOut
from .security import create_media_token, decode_media_token

router = APIRouter(prefix="/v1/media", tags=["media"])
logger = logging.getLogger(__name__)


async def read_upload_chunk(request: Request, maximum_bytes: int) -> bytes:
    parts: list[bytes] = []
    received = 0
    try:
        async for part in request.stream():
            received += len(part)
            if received > maximum_bytes:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "chunk_too_large", "message": "Chunk exceeds the permitted size"},
                )
            parts.append(part)
    except ClientDisconnect as exc:
        logger.info("Upload connection closed after %s bytes", received)
        raise HTTPException(
            status_code=499,
            detail={"code": "upload_interrupted", "message": "Upload connection was interrupted"},
        ) from exc
    return b"".join(parts)


def media_out(asset: MediaAsset, include_chunk_size: bool = False) -> MediaOut:
    settings = get_settings()
    thumbnail_url = None
    if asset.thumbnail_path:
        token = create_media_token(asset.id)
        thumbnail_url = f"{settings.api_public_url}/v1/media/{asset.id}/thumbnail?token={token}"
    return MediaOut(
        id=asset.id,
        original_name=asset.original_name,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        uploaded_bytes=asset.uploaded_bytes,
        duration_seconds=asset.duration_seconds,
        width=asset.width,
        height=asset.height,
        status=asset.status,
        thumbnail_url=thumbnail_url,
        chunk_size=settings.upload_chunk_bytes if include_chunk_size else None,
    )


async def owned_media(db: AsyncSession, user: User, media_id: str, *, lock: bool = False) -> MediaAsset:
    query = (
        select(MediaAsset)
        .where(MediaAsset.id == media_id, MediaAsset.user_id == user.id)
        .execution_options(populate_existing=lock)
    )
    if lock:
        query = query.with_for_update()
    asset = await db.scalar(query)
    if not asset:
        raise HTTPException(
            status_code=404, detail={"code": "media_not_found", "message": "Upload not found"}
        )
    return asset


@router.post("", response_model=MediaOut, status_code=201)
async def initialize_media(
    payload: MediaInitIn, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)
):
    settings = get_settings()
    if payload.size_bytes > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413, detail={"code": "file_too_large", "message": "Videos can be up to 500 MB"}
        )
    suffix = ".mov" if payload.mime_type == "video/quicktime" else ".mp4"
    asset = MediaAsset(
        user_id=user.id,
        original_name=Path(payload.filename).name,
        mime_type=payload.mime_type,
        size_bytes=payload.size_bytes,
        storage_path="pending",
        delete_after=datetime.now(UTC) + timedelta(hours=24),
    )
    db.add(asset)
    await db.flush()
    asset.storage_path = str(settings.storage_root / "uploads" / f"{asset.id}{suffix}")
    await db.commit()
    return media_out(asset, include_chunk_size=True)


@router.get("/{media_id}", response_model=MediaOut)
async def get_media(
    media_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return media_out(await owned_media(db, user, media_id), include_chunk_size=True)


@router.put("/{media_id}", response_model=MediaOut)
async def upload_chunk(
    media_id: str,
    request: Request,
    content_range: str | None = Header(default=None, alias="Content-Range"),
    chunk_sha256: str | None = Header(default=None, alias="X-Chunk-SHA256"),
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    asset = await owned_media(db, user, media_id)
    if asset.status != "uploading":
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_not_active", "message": "This upload is no longer accepting chunks"},
        )
    try:
        start, end, total = parse_content_range(content_range)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_content_range", "message": str(exc)}
        ) from exc
    expected_size = asset.size_bytes
    if total != expected_size:
        raise HTTPException(
            status_code=400, detail={"code": "size_mismatch", "message": "Declared file size changed"}
        )
    # Do not hold a database connection or row lock while a slow request body
    # is still arriving from the browser.
    await db.commit()
    chunk = await read_upload_chunk(request, get_settings().upload_chunk_bytes)
    if not chunk_sha256:
        raise HTTPException(
            status_code=400,
            detail={"code": "checksum_required", "message": "Each chunk requires a SHA-256 checksum"},
        )
    actual_checksum = hashlib.sha256(chunk).hexdigest()
    if not hmac.compare_digest(actual_checksum, chunk_sha256.lower()):
        raise HTTPException(
            status_code=422,
            detail={"code": "checksum_mismatch", "message": "Chunk checksum did not match"},
        )
    asset = await owned_media(db, user, media_id, lock=True)
    if asset.status != "uploading":
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_not_active", "message": "This upload is no longer accepting chunks"},
        )
    if asset.size_bytes != total:
        raise HTTPException(
            status_code=400, detail={"code": "size_mismatch", "message": "Declared file size changed"}
        )
    try:
        asset.uploaded_bytes = await asyncio.to_thread(
            append_chunk,
            Path(asset.storage_path),
            chunk,
            start,
            end,
            total,
            asset.uploaded_bytes,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "invalid_upload_offset", "message": str(exc)}
        ) from exc
    asset.delete_after = datetime.now(UTC) + timedelta(hours=24)
    await db.commit()
    return media_out(asset, include_chunk_size=True)


@router.post("/{media_id}/complete", response_model=MediaOut)
async def complete_media(
    media_id: str, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)
):
    asset = await owned_media(db, user, media_id, lock=True)
    if asset.status == "ready":
        return media_out(asset)
    if asset.uploaded_bytes != asset.size_bytes:
        raise HTTPException(
            status_code=409, detail={"code": "upload_incomplete", "message": "Upload is not complete"}
        )
    path = Path(asset.storage_path)
    try:
        checksum = await asyncio.to_thread(sha256_file, path)
        duration, width, height = await asyncio.to_thread(probe_video, path)
        if duration < 1 or duration > 600:
            raise ValueError("Videos must be between 1 second and 10 minutes")
        thumbnail = get_settings().storage_root / "thumbnails" / f"{asset.id}.jpg"
        await asyncio.to_thread(create_thumbnail, path, thumbnail)
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        asset.status = "failed"
        await db.commit()
        raise HTTPException(status_code=422, detail={"code": "invalid_video", "message": str(exc)}) from exc
    asset.checksum_sha256 = checksum
    asset.duration_seconds = duration
    asset.width = width
    asset.height = height
    asset.thumbnail_path = str(thumbnail)
    asset.status = "ready"
    asset.delete_after = datetime.now(UTC) + timedelta(days=7)
    await db.commit()
    return media_out(asset)


@router.get("/{media_id}/thumbnail", include_in_schema=False)
async def thumbnail(media_id: str, token: str = Query(...), db: AsyncSession = Depends(get_db)):
    try:
        token_media_id = decode_media_token(token)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid media token") from exc
    if token_media_id != media_id:
        raise HTTPException(status_code=403, detail="Media token does not match")
    asset = await db.get(MediaAsset, media_id)
    if not asset or not asset.thumbnail_path or not Path(asset.thumbnail_path).exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(
        asset.thumbnail_path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
    )


@router.delete("/{media_id}", response_model=MessageOut)
async def delete_media(
    media_id: str, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)
):
    asset = await owned_media(db, user, media_id)
    count = await db.scalar(select(func.count(Post.id)).where(Post.media_id == asset.id))
    if count:
        raise HTTPException(
            status_code=409, detail={"code": "media_in_use", "message": "Delete the post first"}
        )
    Path(asset.storage_path).unlink(missing_ok=True)
    if asset.thumbnail_path:
        Path(asset.thumbnail_path).unlink(missing_ok=True)
    await db.delete(asset)
    await db.commit()
    return MessageOut(message="Upload deleted")
