import asyncio
import hashlib
import hmac
import logging
import math
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect

from .config import get_settings
from .database import get_db
from .deps import get_current_user, require_verified_user
from .media import append_chunk, create_thumbnail, parse_content_range, probe_video, sha256_file
from .models import MediaAsset, MediaUploadPart, Post, User
from .schemas import MediaInitIn, MediaOut, MediaPartConfirmIn, MediaPartUrlOut, MessageOut
from .security import create_media_token, decode_media_token
from .storage import R2Storage, StorageError, StoredPart, normalize_etag

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
    if asset.thumbnail_path or asset.thumbnail_key:
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
        chunk_size=(
            settings.r2_multipart_part_size_bytes
            if asset.storage_backend == "r2"
            else settings.upload_chunk_bytes
        )
        if include_chunk_size
        else None,
        upload_mode=asset.storage_backend,
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
        storage_backend=settings.storage_backend,
        delete_after=datetime.now(UTC) + timedelta(hours=24),
    )
    db.add(asset)
    await db.flush()
    asset_id = asset.id
    r2_storage: R2Storage | None = None
    r2_upload_id: str | None = None
    r2_object_key: str | None = None
    try:
        if settings.storage_backend == "r2":
            r2_storage = R2Storage(settings)
            asset.object_key = r2_storage.object_key(user.id, asset.id, suffix)
            r2_object_key = asset.object_key
            r2_upload_id = await r2_storage.create_multipart_upload(
                asset.object_key, payload.mime_type
            )
            asset.multipart_upload_id = r2_upload_id
            asset.storage_path = ""
        else:
            asset.storage_path = str(settings.storage_root / "uploads" / f"{asset.id}{suffix}")
        await db.commit()
    except StorageError as exc:
        await db.rollback()
        if r2_storage and r2_upload_id and r2_object_key:
            try:
                await r2_storage.abort_multipart_upload(r2_object_key, r2_upload_id)
            except StorageError:
                logger.warning("Could not abort failed R2 upload initialization for media %s", asset_id)
        raise HTTPException(
            status_code=503,
            detail={"code": "storage_unavailable", "message": "Media storage is temporarily unavailable"},
        ) from exc
    return media_out(asset, include_chunk_size=True)


@router.get("/{media_id}", response_model=MediaOut)
async def get_media(
    media_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    return media_out(await owned_media(db, user, media_id), include_chunk_size=True)


def r2_part_count(asset: MediaAsset, settings) -> int:
    return math.ceil(asset.size_bytes / settings.r2_multipart_part_size_bytes)


def r2_expected_part_size(asset: MediaAsset, part_number: int, settings) -> int:
    part_count = r2_part_count(asset, settings)
    if part_number < 1 or part_number > part_count:
        raise ValueError("Invalid R2 part number")
    if part_number == part_count:
        return asset.size_bytes - (part_count - 1) * settings.r2_multipart_part_size_bytes
    return settings.r2_multipart_part_size_bytes


async def stored_r2_parts(db: AsyncSession, asset: MediaAsset) -> list[StoredPart]:
    settings = get_settings()
    records = list(
        await db.scalars(
            select(MediaUploadPart)
            .where(MediaUploadPart.media_id == asset.id)
            .order_by(MediaUploadPart.part_number)
        )
    )
    expected_count = r2_part_count(asset, settings)
    if len(records) != expected_count:
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_incomplete", "message": "Upload is missing one or more parts"},
        )
    parts: list[StoredPart] = []
    next_offset = 0
    for expected_number, record in enumerate(records, start=1):
        if record.part_number != expected_number:
            raise HTTPException(
                status_code=409,
                detail={"code": "upload_incomplete", "message": "Upload parts are not contiguous"},
            )
        try:
            expected_size = r2_expected_part_size(asset, record.part_number, settings)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "upload_incomplete", "message": "Upload contains an invalid part"},
            ) from exc
        if record.size_bytes != expected_size:
            raise HTTPException(
                status_code=409,
                detail={"code": "upload_incomplete", "message": "Upload part size is invalid"},
            )
        next_offset += record.size_bytes
        parts.append(
            StoredPart(
                part_number=record.part_number,
                etag=normalize_etag(record.etag),
                size_bytes=record.size_bytes,
            )
        )
    if next_offset != asset.size_bytes:
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_incomplete", "message": "Upload is not complete"},
        )
    return parts


@router.post("/{media_id}/parts/{part_number}/sign", response_model=MediaPartUrlOut)
async def sign_upload_part(
    media_id: str,
    part_number: int,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    asset = await owned_media(db, user, media_id)
    if asset.storage_backend != "r2":
        raise HTTPException(
            status_code=409,
            detail={"code": "direct_upload_unavailable", "message": "This upload uses the local upload path"},
        )
    if asset.status != "uploading":
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_not_active", "message": "This upload is no longer accepting parts"},
        )
    if not asset.object_key or not asset.multipart_upload_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_not_active", "message": "This multipart upload is no longer active"},
        )
    try:
        r2_expected_part_size(asset, part_number, settings)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_part_number", "message": "The requested upload part does not exist"},
        ) from exc
    try:
        url = await R2Storage(settings).presign_part(
            asset.object_key, asset.multipart_upload_id, part_number
        )
    except StorageError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "storage_unavailable", "message": "Media storage is temporarily unavailable"},
        ) from exc
    return MediaPartUrlOut(
        part_number=part_number,
        url=url,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.r2_presign_ttl_seconds),
    )


@router.post("/{media_id}/parts/{part_number}/confirm", response_model=MediaOut)
async def confirm_upload_part(
    media_id: str,
    part_number: int,
    payload: MediaPartConfirmIn,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    if payload.part_number != part_number:
        raise HTTPException(
            status_code=422,
            detail={"code": "part_number_mismatch", "message": "Part number does not match the request"},
        )
    asset = await owned_media(db, user, media_id)
    if asset.storage_backend != "r2":
        raise HTTPException(
            status_code=409,
            detail={"code": "direct_upload_unavailable", "message": "This upload uses the local upload path"},
        )
    if asset.status != "uploading":
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_not_active", "message": "This upload is no longer accepting parts"},
        )
    if not asset.object_key or not asset.multipart_upload_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_not_active", "message": "This multipart upload is no longer active"},
        )
    try:
        expected_size = r2_expected_part_size(asset, part_number, settings)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_part_number", "message": "The uploaded part does not exist"},
        ) from exc
    if payload.size_bytes != expected_size:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_part_size", "message": "The uploaded part has an invalid size"},
        )
    try:
        remote_parts = await R2Storage(settings).list_parts(asset.object_key, asset.multipart_upload_id)
    except StorageError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "storage_unavailable", "message": "Media storage is temporarily unavailable"},
        ) from exc
    remote = next((part for part in remote_parts if part.part_number == part_number), None)
    if not remote or normalize_etag(remote.etag) != normalize_etag(payload.etag) or remote.size_bytes != payload.size_bytes:
        raise HTTPException(
            status_code=409,
            detail={"code": "part_confirmation_failed", "message": "R2 did not confirm this upload part"},
        )

    asset = await owned_media(db, user, media_id, lock=True)
    if asset.storage_backend != "r2" or asset.status != "uploading":
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_not_active", "message": "This upload is no longer accepting parts"},
        )
    record = await db.scalar(
        select(MediaUploadPart).where(
            MediaUploadPart.media_id == asset.id,
            MediaUploadPart.part_number == part_number,
        )
    )
    if record:
        record.etag = normalize_etag(payload.etag)
        record.size_bytes = payload.size_bytes
    else:
        db.add(
            MediaUploadPart(
                media_id=asset.id,
                part_number=part_number,
                etag=normalize_etag(payload.etag),
                size_bytes=payload.size_bytes,
            )
        )
    await db.flush()
    records = list(
        await db.scalars(
            select(MediaUploadPart)
            .where(MediaUploadPart.media_id == asset.id)
            .order_by(MediaUploadPart.part_number)
        )
    )
    uploaded_bytes = 0
    for expected_number, item in enumerate(records, start=1):
        if item.part_number != expected_number:
            break
        if item.size_bytes != r2_expected_part_size(asset, item.part_number, settings):
            break
        uploaded_bytes += item.size_bytes
    asset.uploaded_bytes = uploaded_bytes
    asset.delete_after = datetime.now(UTC) + timedelta(hours=24)
    await db.commit()
    return media_out(asset, include_chunk_size=True)


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
    if asset.storage_backend != "local":
        raise HTTPException(
            status_code=409,
            detail={"code": "direct_upload_required", "message": "Use the signed multipart upload for this media"},
        )
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
    if asset.storage_backend == "r2":
        if not asset.object_key:
            raise HTTPException(
                status_code=409,
                detail={"code": "upload_not_active", "message": "The uploaded object is missing"},
            )
        settings = get_settings()
        try:
            parts = await stored_r2_parts(db, asset)
        except HTTPException:
            raise
        try:
            storage = R2Storage(settings)
            if asset.multipart_upload_id:
                remote_parts = await storage.list_parts(asset.object_key, asset.multipart_upload_id)
                remote_by_number = {part.part_number: part for part in remote_parts}
                if any(
                    remote_by_number.get(part.part_number) != part
                    for part in parts
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "part_confirmation_failed",
                            "message": "The uploaded parts changed before completion",
                        },
                    )
                await storage.complete_multipart_upload(asset.object_key, asset.multipart_upload_id, parts)
                asset.multipart_upload_id = None
                await db.commit()
        except StorageError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "storage_unavailable", "message": "Media storage is temporarily unavailable"},
            ) from exc

        temp_dir = Path(tempfile.mkdtemp(prefix="reverb-media-processing-", dir=settings.storage_root))
        path = temp_dir / f"{asset.id}{'.mov' if asset.mime_type == 'video/quicktime' else '.mp4'}"
        thumbnail = temp_dir / f"{asset.id}.jpg"
        thumbnail_key = asset.thumbnail_key or storage.thumbnail_key(asset.user_id, asset.id)
        try:
            await storage.download_to_path(asset.object_key, path)
            checksum = await asyncio.to_thread(sha256_file, path)
            duration, width, height = await asyncio.to_thread(probe_video, path)
            if duration < 1 or duration > 600:
                raise ValueError("Videos must be between 1 second and 10 minutes")
            await asyncio.to_thread(create_thumbnail, path, thumbnail)
            await storage.put_file(thumbnail, thumbnail_key, "image/jpeg")
        except StorageError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "storage_unavailable", "message": "Media storage is temporarily unavailable"},
            ) from exc
        except (RuntimeError, ValueError, KeyError, OSError) as exc:
            asset.status = "failed"
            asset.delete_after = datetime.now(UTC) + timedelta(days=7)
            await db.commit()
            raise HTTPException(status_code=422, detail={"code": "invalid_video", "message": str(exc)}) from exc
        finally:
            # The temporary directory is removed explicitly because the source
            # video may be hundreds of megabytes and must not remain on /data.
            await asyncio.to_thread(shutil.rmtree, temp_dir, True)
        asset.checksum_sha256 = checksum
        asset.duration_seconds = duration
        asset.width = width
        asset.height = height
        asset.thumbnail_key = thumbnail_key
        asset.status = "ready"
        asset.delete_after = datetime.now(UTC) + timedelta(days=7)
        await db.commit()
        return media_out(asset)

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
    if not asset:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    if asset.storage_backend == "r2":
        if not asset.thumbnail_key:
            raise HTTPException(status_code=404, detail="Thumbnail not found")
        try:
            body = await R2Storage().read_bytes(asset.thumbnail_key)
        except StorageError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "storage_unavailable", "message": "Media storage is temporarily unavailable"},
            ) from exc
        return Response(
            content=body,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )
    if not asset.thumbnail_path or not Path(asset.thumbnail_path).exists():
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
    if asset.storage_backend == "r2":
        try:
            storage = R2Storage()
            if asset.multipart_upload_id and asset.object_key:
                await storage.abort_multipart_upload(asset.object_key, asset.multipart_upload_id)
            await storage.delete_object(asset.object_key)
            await storage.delete_object(asset.thumbnail_key)
        except StorageError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "storage_unavailable", "message": "Media storage is temporarily unavailable"},
            ) from exc
    else:
        if asset.storage_path:
            Path(asset.storage_path).unlink(missing_ok=True)
        if asset.thumbnail_path:
            Path(asset.thumbnail_path).unlink(missing_ok=True)
    await db.delete(asset)
    await db.commit()
    return MessageOut(message="Upload deleted")
