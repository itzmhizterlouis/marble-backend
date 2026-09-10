from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .config import Settings, get_settings


class StorageError(OSError):
    """An operational storage failure that is safe to retry."""

    def __init__(self, message: str = "Media storage is temporarily unavailable"):
        super().__init__(message)


@dataclass(frozen=True)
class StoredPart:
    part_number: int
    etag: str
    size_bytes: int


def normalize_etag(value: str) -> str:
    return value.strip().strip('"')


def quoted_etag(value: str) -> str:
    return f'"{normalize_etag(value)}"'


class R2Storage:
    backend = "r2"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        try:
            self.client = boto3.client(
                "s3",
                endpoint_url=self.settings.r2_endpoint,
                aws_access_key_id=self.settings.r2_access_key_id,
                aws_secret_access_key=self.settings.r2_secret_access_key,
                region_name=self.settings.r2_region,
            )
        except (BotoCoreError, ClientError, OSError, ValueError) as exc:
            raise StorageError() from exc

    @property
    def bucket(self) -> str:
        return self.settings.r2_bucket_name

    @staticmethod
    def object_key(user_id: str, media_id: str, suffix: str) -> str:
        return f"users/{user_id}/media/{media_id}/source{suffix}"

    @staticmethod
    def thumbnail_key(user_id: str, media_id: str) -> str:
        return f"users/{user_id}/media/{media_id}/thumbnail.jpg"

    async def _call(self, operation: str, **kwargs: Any) -> dict:
        def invoke() -> dict:
            try:
                return getattr(self.client, operation)(**kwargs)
            except (BotoCoreError, ClientError, OSError, ValueError) as exc:
                raise StorageError() from exc

        return await asyncio.to_thread(invoke)

    async def create_multipart_upload(self, key: str, content_type: str) -> str:
        response = await self._call(
            "create_multipart_upload",
            Bucket=self.bucket,
            Key=key,
            ContentType=content_type,
        )
        upload_id = response.get("UploadId")
        if not upload_id:
            raise StorageError("R2 did not return a multipart upload ID")
        return str(upload_id)

    async def presign_part(self, key: str, upload_id: str, part_number: int) -> str:
        def generate() -> str:
            try:
                return self.client.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": self.bucket,
                        "Key": key,
                        "UploadId": upload_id,
                        "PartNumber": part_number,
                    },
                    ExpiresIn=self.settings.r2_presign_ttl_seconds,
                    HttpMethod="PUT",
                )
            except (BotoCoreError, ClientError, OSError, ValueError) as exc:
                raise StorageError() from exc

        return await asyncio.to_thread(generate)

    async def list_parts(self, key: str, upload_id: str) -> list[StoredPart]:
        parts: list[StoredPart] = []
        marker: int | None = None
        while True:
            params: dict[str, Any] = {
                "Bucket": self.bucket,
                "Key": key,
                "UploadId": upload_id,
            }
            if marker is not None:
                params["PartNumberMarker"] = marker
            response = await self._call("list_parts", **params)
            parts.extend(
                StoredPart(
                    part_number=int(item["PartNumber"]),
                    etag=normalize_etag(str(item["ETag"])),
                    size_bytes=int(item["Size"]),
                )
                for item in response.get("Parts", [])
            )
            if not response.get("IsTruncated"):
                return parts
            next_marker = response.get("NextPartNumberMarker")
            if next_marker is None:
                return parts
            marker = int(next_marker)

    async def complete_multipart_upload(
        self, key: str, upload_id: str, parts: list[StoredPart]
    ) -> None:
        await self._call(
            "complete_multipart_upload",
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": part.part_number, "ETag": quoted_etag(part.etag)}
                    for part in sorted(parts, key=lambda item: item.part_number)
                ]
            },
        )

    async def object_size(self, key: str) -> int | None:
        """Return the completed object size, or None when it does not exist."""

        def head() -> int | None:
            try:
                response = self.client.head_object(Bucket=self.bucket, Key=key)
                return int(response["ContentLength"])
            except ClientError as exc:
                status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                    return None
                raise StorageError() from exc
            except (BotoCoreError, OSError, ValueError, KeyError) as exc:
                raise StorageError() from exc

        return await asyncio.to_thread(head)

    async def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        await self._call(
            "abort_multipart_upload",
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
        )

    async def download_to_path(self, key: str, destination: Path) -> None:
        def download() -> None:
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=key)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as handle:
                    body = response["Body"]
                    try:
                        for chunk in iter(lambda: body.read(8 * 1024 * 1024), b""):
                            handle.write(chunk)
                    finally:
                        body.close()
            except (BotoCoreError, ClientError, OSError, ValueError) as exc:
                raise StorageError() from exc

        await asyncio.to_thread(download)

    async def put_file(self, source: Path, key: str, content_type: str) -> None:
        def upload() -> None:
            try:
                with source.open("rb") as handle:
                    self.client.put_object(
                        Bucket=self.bucket,
                        Key=key,
                        Body=handle,
                        ContentType=content_type,
                    )
            except (BotoCoreError, ClientError, OSError, ValueError) as exc:
                raise StorageError() from exc

        await asyncio.to_thread(upload)

    async def read_bytes(self, key: str) -> bytes:
        def read() -> bytes:
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=key)
                body = response["Body"]
                try:
                    return body.read()
                finally:
                    body.close()
            except (BotoCoreError, ClientError, OSError, ValueError) as exc:
                raise StorageError() from exc

        return await asyncio.to_thread(read)

    async def delete_object(self, key: str | None) -> None:
        if not key:
            return
        await self._call("delete_object", Bucket=self.bucket, Key=key)


@asynccontextmanager
async def materialize_object(
    *,
    storage_backend: str,
    object_key: str | None,
    storage_path: str,
    filename: str,
    settings: Settings | None = None,
) -> AsyncIterator[Path]:
    """Yield a local path for provider/FFmpeg consumers and clean R2 temps."""
    if storage_backend != "r2":
        yield Path(storage_path)
        return
    if not object_key:
        raise StorageError("Media object key is missing")
    active_settings = settings or get_settings()
    temp_dir = Path(tempfile.mkdtemp(prefix="reverb-media-", dir=active_settings.storage_root))
    destination = temp_dir / Path(filename).name
    try:
        await R2Storage(active_settings).download_to_path(object_key, destination)
        yield destination
    finally:
        await asyncio.to_thread(shutil.rmtree, temp_dir, True)
