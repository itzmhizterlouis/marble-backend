import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


def parse_content_range(value: str | None) -> tuple[int, int, int]:
    match = CONTENT_RANGE.match(value or "")
    if not match:
        raise ValueError("Content-Range must use bytes start-end/total")
    start, end, total = map(int, match.groups())
    if start < 0 or end < start or end >= total:
        raise ValueError("Invalid Content-Range bounds")
    return start, end, total


def append_chunk(path: Path, chunk: bytes, start: int, end: int, total: int, expected_offset: int) -> int:
    if start != expected_offset:
        raise ValueError(f"Expected byte offset {expected_offset}")
    if len(chunk) != end - start + 1:
        raise ValueError("Chunk length does not match Content-Range")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(chunk)
    next_offset = end + 1
    if next_offset > total:
        raise ValueError("Upload exceeds declared size")
    return next_offset


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_video(path: Path) -> tuple[int, int, int]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is required to validate uploaded media")
    result = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    return round(float(payload["format"]["duration"])), int(stream["width"]), int(stream["height"])


def create_thumbnail(path: Path, output: Path) -> None:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg is required to create thumbnails")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            executable,
            "-y",
            "-ss",
            "00:00:01",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=720:-2",
            str(output),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
