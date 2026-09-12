from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from .config import get_settings


class GeminiError(RuntimeError):
    pass


CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "shared_caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "tiktok_caption": {"type": "string"},
        "instagram_caption": {"type": "string"},
        "facebook_caption": {"type": "string"},
        "youtube_title": {"type": "string"},
        "youtube_description": {"type": "string"},
    },
    "required": [
        "shared_caption",
        "hashtags",
        "tiktok_caption",
        "instagram_caption",
        "facebook_caption",
        "youtube_title",
        "youtube_description",
    ],
}


class GeminiClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.gemini_api_key:
            raise GeminiError("Gemini is not configured")

    async def _file_stream(self, path: Path):
        with path.open("rb") as source:
            while chunk := await asyncio.to_thread(source.read, 4 * 1024 * 1024):
                yield chunk

    async def upload_file(self, path: Path, mime_type: str) -> dict:
        start_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={self.settings.gemini_api_key}"
        size = path.stat().st_size
        headers = {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(start_url, headers=headers, json={"file": {"display_name": path.name}})
            if response.is_error or not response.headers.get("X-Goog-Upload-URL"):
                raise GeminiError("Gemini could not accept this video")
            upload_url = response.headers["X-Goog-Upload-URL"]
            response = await client.post(
                upload_url,
                headers={
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize",
                    "Content-Length": str(size),
                    "Content-Type": mime_type,
                },
                content=self._file_stream(path),
                timeout=None,
            )
            if response.is_error:
                raise GeminiError("Gemini video upload failed")
            body = response.json()
            file = body.get("file") or body
            for _ in range(60):
                state = str(file.get("state") or "ACTIVE")
                if state == "ACTIVE":
                    return file
                if state == "FAILED":
                    raise GeminiError("Gemini could not process this video")
                await asyncio.sleep(2)
                file_response = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/{file['name']}?key={self.settings.gemini_api_key}"
                )
                if file_response.is_error:
                    raise GeminiError("Gemini video processing failed")
                file = file_response.json()
        raise GeminiError("Gemini video processing timed out")

    async def delete_file(self, name: str) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.delete(
                f"https://generativelanguage.googleapis.com/v1beta/{name}?key={self.settings.gemini_api_key}"
            )

    async def _generate(self, parts: list[dict]) -> tuple[dict, dict]:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.settings.gemini_model}:generateContent?key={self.settings.gemini_api_key}"
        )
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": CANDIDATE_SCHEMA,
                "temperature": 0.7,
            },
        }
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(url, json=payload)
        if response.is_error:
            raise GeminiError("AI creation is temporarily unavailable")
        body = response.json()
        try:
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            candidate = json.loads(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise GeminiError("AI returned an invalid draft") from exc
        if not all(key in candidate for key in CANDIDATE_SCHEMA["required"]):
            raise GeminiError("AI returned an incomplete draft")
        return candidate, body.get("usageMetadata") or {}

    async def create_candidate(
        self,
        *,
        file: dict,
        current_caption: str,
        hashtags: list[str],
        platforms: list[str],
        generation_context: str = "",
    ) -> tuple[dict, dict]:
        context_instruction = (
            f"Creator-provided additional context (use as guidance, preserve the facts, and do not invent details): "
            f"{generation_context!r}."
            if generation_context.strip()
            else "No additional creator context was provided."
        )
        prompt = (
            "Create accurate, engaging social copy for the attached creator video. Do not invent factual claims. "
            f"Selected platforms: {', '.join(platforms)}. Existing caption: {current_caption!r}. "
            f"Existing hashtags: {', '.join(hashtags)}. {context_instruction} "
            "Keep each platform's conventions and return only the schema."
        )
        return await self._generate(
            [
                {"fileData": {"mimeType": file.get("mimeType") or file.get("mime_type") or "video/mp4", "fileUri": file["uri"]}},
                {"text": prompt},
            ]
        )

    async def adjust_candidate(
        self,
        candidate: dict,
        adjustment: str,
        generation_context: str = "",
    ) -> tuple[dict, dict]:
        context_instruction = (
            f"Keep following this creator-provided context (use as guidance, preserve facts, and do not invent details): "
            f"{generation_context!r}."
            if generation_context.strip()
            else "No additional creator context was provided."
        )
        prompt = (
            f"Rewrite this social content with the adjustment '{adjustment}'. Preserve facts and return every field. "
            f"{context_instruction} Candidate JSON: {json.dumps(candidate, ensure_ascii=False)}"
        )
        return await self._generate([{"text": prompt}])
