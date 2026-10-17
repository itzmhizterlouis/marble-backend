"""Word-timed transcription and deterministic, burned-in subtitle video versions."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import MediaAsset, Post, SubtitleJob
from .providers import ProviderError


class SubtitleError(ValueError):
    """A safe, user-visible processing error (never provider response bodies)."""


def clean_word(value: str) -> str:
    return " ".join(value.split()).strip()


def normalize_words(payload: dict, duration: float) -> list[dict]:
    words = []
    previous = 0.0
    raw = payload.get("words")
    if not isinstance(raw, list) or len(raw) > 4000:
        raise SubtitleError("We couldn’t time the spoken words. Please try again.")
    for item in raw:
        try:
            start, end = float(item["start"]), float(item["end"])
            text = clean_word(item["word"])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SubtitleError("We couldn’t time the spoken words. Please try again.") from exc
        if not all(math.isfinite(t) for t in (start, end)):
            raise SubtitleError("We couldn’t time the spoken words. Please try again.")
        start, end = max(previous, start, 0), min(end, duration)
        if not text or end <= start:
            continue
        if len(text) > 80:
            raise SubtitleError("A subtitle word is too long. Please try again.")
        words.append({"text": text, "start": round(start, 3), "end": round(end, 3)})
        previous = end
    if not words:
        raise SubtitleError("No speech was detected. You can continue without subtitles.")
    return words


def extract_audio(source: Path, destination: Path) -> None:
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "flac",
                str(destination),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        raise SubtitleError("We couldn’t read the audio. Check that your video contains speech.") from exc


async def transcribe(audio: Path, duration: float) -> list[dict]:
    key = get_settings().groq_api_key
    if not key:
        raise SubtitleError("Auto subtitles are temporarily unavailable.")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as client:
            with audio.open("rb") as handle:
                response = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    data={
                        "model": "whisper-large-v3-turbo",
                        "response_format": "verbose_json",
                        "timestamp_granularities[]": "word",
                        "temperature": "0",
                    },
                    files={"file": ("audio.flac", handle, "audio/flac")},
                )
        if response.status_code == 429:
            raise SubtitleError("Subtitles are busy right now. Please retry shortly.")
        if response.is_error:
            raise SubtitleError("We couldn’t generate subtitles. Please try again.")
        return normalize_words(response.json(), duration)
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise SubtitleError("We couldn’t reach the transcription service. Please try again.") from exc


def ass_time(seconds: float) -> str:
    centis = round(seconds * 100)
    return f"{centis // 360000}:{centis // 6000 % 60:02}:{centis // 100 % 60:02}.{centis % 100:02}"


def ass_text(text: str) -> str:
    # ASS has no reliable universal escape for override braces. Literal fullwidth
    # forms preserve visible text without allowing user input to inject commands.
    return clean_word(text).replace("\\", "＼").replace("{", "｛").replace("}", "｝")


def make_ass(words: list[dict], width: int, height: int) -> str:
    font_size = max(18, round(min(width * 0.058, height * 0.075)))
    margin = round(height * (0.23 if height > width else 0.16))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans,{font_size},&H00FFFFFF,&H00FFFFFF,&H00181818,&H80000000,-1,0,0,0,100,100,0,0,1,{max(2, round(font_size * 0.065))},1,2,{round(width * 0.1)},{round(width * 0.1)},{margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    groups: list[list[dict]] = []
    group: list[dict] = []
    for word in words:
        if group and (
            len(group) >= 3
            or sum(len(w["text"]) + 1 for w in group) + len(word["text"]) > 26
            or word["start"] - group[-1]["end"] > 0.5
        ):
            groups.append(group)
            group = []
        group.append(word)
    if group:
        groups.append(group)
    lines = []
    for group in groups:
        # Long corrected names still fit the safe horizontal area.
        chars = sum(len(w["text"]) + 1 for w in group)
        size = min(font_size, max(12, int(width * 0.72 / max(1, chars) / 0.65)))
        for index, word in enumerate(group):
            end = (
                min(group[index + 1]["start"], word["end"] + 0.25) if index + 1 < len(group) else word["end"]
            )
            text = " ".join(
                (r"{\fscx112\fscy112}" if i == index else r"{\fscx100\fscy100}") + ass_text(w["text"])
                for i, w in enumerate(group)
            )
            lines.append(
                f"Dialogue: 0,{ass_time(word['start'])},{ass_time(end)},Default,,0,0,0,,{{\\fs{size}}}{text}"
            )
    return header + "\n".join(lines) + "\n"


def render_video(source: Path, output: Path, words: list[dict]) -> None:
    """Autorotate first, and size subtitles against the actual display dimensions."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-of", "json", str(source)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    stream = json.loads(result.stdout)["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])
    rotation = next((float(s["rotation"]) for s in stream.get("side_data_list", []) if "rotation" in s), 0)
    if round(rotation) % 180:
        width, height = height, width
    scale = min(1, 1920 / max(width, height))
    width, height = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
    script = output.parent / "captions.ass"
    script.write_text(make_ass(words, width, height), encoding="utf-8")
    # Fixed relative script name avoids FFmpeg filter-path quoting/injection.
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source.resolve()),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            f"scale={width}:{height},setsar=1,ass=captions.ass",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-maxrate",
            "4M",
            "-bufsize",
            "8M",
            "-threads",
            "2",
            "-filter_threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            str(output.resolve()),
        ],
        cwd=output.parent,
        check=True,
        capture_output=True,
        timeout=1200,
    )


async def selected_video(db: AsyncSession, post: Post) -> MediaAsset | SimpleNamespace:
    """One selection shared by immediate/scheduled submissions and targeted retries."""
    if not post.subtitle_job_id:
        return post.media
    job = await db.get(SubtitleJob, post.subtitle_job_id)
    if not job or job.media_id != post.media_id or job.user_id != post.user_id or job.status != "ready":
        raise ProviderError(
            "subtitles_not_ready", "Wait for subtitles, or turn them off before publishing", 409
        )
    if not (job.object_key if post.media.storage_backend == "r2" else job.storage_path):
        raise ProviderError(
            "subtitles_expired", "This subtitle video has expired. Upload the video again", 409
        )
    return SimpleNamespace(
        storage_backend=post.media.storage_backend,
        object_key=job.object_key,
        storage_path=job.storage_path,
        original_name="subtitled.mp4",
        size_bytes=job.size_bytes,
        duration_seconds=post.media.duration_seconds,
        width=job.width,
        height=job.height,
    )


async def delete_subtitle_files(db: AsyncSession, asset: MediaAsset) -> None:
    from sqlalchemy import select

    from .storage import R2Storage

    jobs = list(await db.scalars(select(SubtitleJob).where(SubtitleJob.media_id == asset.id)))
    for job in jobs:
        if asset.storage_backend == "r2" and job.object_key:
            await R2Storage().delete_object(job.object_key)
        elif job.storage_path:
            Path(job.storage_path).unlink(missing_ok=True)
            shutil.rmtree(Path(job.storage_path).parent, ignore_errors=True)
        job.object_key = job.storage_path = None
        job.status = "expired"
