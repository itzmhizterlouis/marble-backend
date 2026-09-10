from pathlib import Path

from app.config import Settings
from app.storage import (
    R2Storage,
    StoredPart,
    materialize_object,
    normalize_etag,
    quoted_etag,
)


class FakeR2Client:
    def __init__(self):
        self.completed = None

    def create_multipart_upload(self, **_kwargs):
        return {"UploadId": "upload-1"}

    def generate_presigned_url(self, operation, **kwargs):
        assert operation == "upload_part"
        assert kwargs["Params"]["Bucket"] == "reverb"
        assert kwargs["Params"]["PartNumber"] == 1
        return "https://r2.example/signed-part"

    def list_parts(self, **_kwargs):
        return {"Parts": [{"PartNumber": 1, "ETag": '"etag-1"', "Size": 8}]}

    def complete_multipart_upload(self, **kwargs):
        self.completed = kwargs
        return {}


def r2_settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        storage_backend="r2",
        storage_root=tmp_path,
        r2_endpoint="https://account.r2.cloudflarestorage.com/",
        r2_bucket_name="reverb",
        r2_access_key_id="access-key",
        r2_secret_access_key="secret-key",
    )


async def test_r2_multipart_wrapper_preserves_completion_etag(monkeypatch, tmp_path):
    client = FakeR2Client()
    monkeypatch.setattr("app.storage.boto3.client", lambda *_args, **_kwargs: client)
    storage = R2Storage(r2_settings(tmp_path))

    assert normalize_etag(' "etag-1" ') == "etag-1"
    assert quoted_etag(' "etag-1" ') == '"etag-1"'
    assert await storage.create_multipart_upload("source.mp4", "video/mp4") == "upload-1"
    assert await storage.presign_part("source.mp4", "upload-1", 1) == "https://r2.example/signed-part"
    parts = await storage.list_parts("source.mp4", "upload-1")
    assert parts == [StoredPart(part_number=1, etag="etag-1", size_bytes=8)]

    await storage.complete_multipart_upload("source.mp4", "upload-1", parts)
    assert client.completed["MultipartUpload"]["Parts"] == [
        {"PartNumber": 1, "ETag": '"etag-1"'}
    ]


async def test_materialize_object_cleans_temporary_r2_download(monkeypatch, tmp_path):
    class FakeStorage:
        def __init__(self, _settings):
            pass

        async def download_to_path(self, _key, destination):
            destination.write_bytes(b"video")

    monkeypatch.setattr("app.storage.R2Storage", FakeStorage)
    settings = r2_settings(tmp_path)

    async with materialize_object(
        storage_backend="r2",
        object_key="source.mp4",
        storage_path="",
        filename="../clip.mp4",
        settings=settings,
    ) as path:
        temporary_directory = path.parent
        assert path.name == "clip.mp4"
        assert path.read_bytes() == b"video"
        assert temporary_directory.exists()

    assert not temporary_directory.exists()
