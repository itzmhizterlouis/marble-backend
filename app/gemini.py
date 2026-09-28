from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import httpx

from .config import get_settings


class GeminiError(RuntimeError):
    def __init__(self, message: str, *, code: str = "gemini_error", status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _request_error(exc: httpx.RequestError, operation: str) -> GeminiError:
    if isinstance(exc, httpx.TimeoutException):
        return GeminiError(
            "Gemini took too long to respond. Please try again.",
            code=f"gemini_{operation}_timeout",
        )
    return GeminiError(
        "Could not reach Gemini. Please try again.",
        code=f"gemini_{operation}_network_error",
    )


def _response_error(response: httpx.Response, operation: str, message: str) -> GeminiError:
    if response.status_code == 429:
        message = "Gemini is busy right now. Please try again shortly."
        code = f"gemini_{operation}_rate_limited"
    elif response.status_code >= 500:
        message = "Gemini is temporarily unavailable. Please try again."
        code = f"gemini_{operation}_server_error"
    else:
        code = f"gemini_{operation}_http_error"
    return GeminiError(message, code=code, status_code=response.status_code)


CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "shared_caption": {"type": "string", "maxLength": 2200},
        "hashtags": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "maxLength": 100},
        },
        "tiktok_caption": {"type": "string", "maxLength": 2200},
        "instagram_caption": {"type": "string", "maxLength": 2200},
        "facebook_title": {"type": "string", "maxLength": 255},
        "facebook_caption": {"type": "string", "maxLength": 63206},
        "youtube_title": {"type": "string", "maxLength": 100},
        "youtube_description": {"type": "string", "maxLength": 5000},
    },
    "required": [
        "shared_caption",
        "hashtags",
        "tiktok_caption",
        "instagram_caption",
        "facebook_title",
        "facebook_caption",
        "youtube_title",
        "youtube_description",
    ],
}

VIDEO_OBSERVATIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "maxLength": 600},
        "details": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string", "maxLength": 220},
        },
        "uncertainties": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "maxLength": 220},
        },
    },
    "required": ["summary", "details", "uncertainties"],
}

VIDEO_GENERATION_SCHEMA = {
    "type": "object",
    "properties": {**CANDIDATE_SCHEMA["properties"], "video_observations": VIDEO_OBSERVATIONS_SCHEMA},
    "required": [*CANDIDATE_SCHEMA["required"], "video_observations"],
}


def validate_video_observations(value: object) -> dict:
    if not isinstance(value, dict):
        raise GeminiError("AI did not describe what it observed in the video")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 600:
        raise GeminiError("AI did not describe what it observed in the video")
    result = {"summary": summary.strip()}
    for name, limit in (("details", 6), ("uncertainties", 3)):
        items = value.get(name)
        if not isinstance(items, list) or len(items) > limit:
            raise GeminiError("AI returned invalid video observations")
        if any(not isinstance(item, str) or len(item) > 220 for item in items):
            raise GeminiError("AI returned invalid video observations")
        result[name] = [item.strip() for item in items if item.strip()]
    return result

ANALYTICS_INSIGHTS_SCHEMA = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "explanation": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": [
                    "candidate_id",
                    "explanation",
                    "action",
                ],
            },
        }
    },
    "required": ["insights"],
}


class GeminiClient:
    def __init__(self, on_stage: Callable[[str], None] | None = None) -> None:
        self.settings = get_settings()
        self.on_stage = on_stage
        if not self.settings.gemini_api_key:
            raise GeminiError("Gemini is not configured")

    def _stage(self, name: str) -> None:
        if self.on_stage:
            self.on_stage(name)

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
            self._stage("gemini_upload_session")
            try:
                response = await client.post(start_url, headers=headers, json={"file": {"display_name": path.name}})
            except httpx.RequestError as exc:
                raise _request_error(exc, "upload_session") from exc
            if response.is_error or not response.headers.get("X-Goog-Upload-URL"):
                raise _response_error(response, "upload_session", "Gemini could not accept this video")
            upload_url = response.headers["X-Goog-Upload-URL"]
            self._stage("gemini_upload")
            try:
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
            except httpx.RequestError as exc:
                raise _request_error(exc, "upload") from exc
            if response.is_error:
                raise _response_error(response, "upload", "Gemini video upload failed")
            try:
                body = response.json()
            except ValueError as exc:
                raise GeminiError("Gemini returned an invalid video response", code="gemini_upload_invalid_response") from exc
            file = body.get("file") or body
            self._stage("gemini_processing")
            for _ in range(60):
                state = str(file.get("state") or "ACTIVE")
                if state == "ACTIVE":
                    return file
                if state == "FAILED":
                    raise GeminiError("Gemini could not process this video", code="gemini_processing_failed")
                await asyncio.sleep(2)
                try:
                    file_response = await client.get(
                        f"https://generativelanguage.googleapis.com/v1beta/{file['name']}?key={self.settings.gemini_api_key}"
                    )
                except httpx.RequestError as exc:
                    raise _request_error(exc, "processing") from exc
                if file_response.is_error:
                    raise _response_error(file_response, "processing", "Gemini video processing failed")
                try:
                    file = file_response.json()
                except ValueError as exc:
                    raise GeminiError("Gemini returned an invalid video response", code="gemini_processing_invalid_response") from exc
        raise GeminiError("Gemini video processing timed out", code="gemini_processing_timeout")

    async def delete_file(self, name: str) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.delete(
                f"https://generativelanguage.googleapis.com/v1beta/{name}?key={self.settings.gemini_api_key}"
            )

    async def _generate_json(
        self,
        parts: list[dict],
        schema: dict,
        *,
        temperature: float,
        invalid_message: str,
    ) -> tuple[dict, dict]:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.settings.gemini_model}:generateContent?key={self.settings.gemini_api_key}"
        )
        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "temperature": temperature,
            },
        }
        self._stage("gemini_generation")
        async with httpx.AsyncClient(timeout=180) as client:
            try:
                response = await client.post(url, json=payload)
            except httpx.RequestError as exc:
                raise _request_error(exc, "generation") from exc
        if response.is_error:
            raise _response_error(response, "generation", "AI creation is temporarily unavailable")
        try:
            body = response.json()
        except ValueError as exc:
            raise GeminiError("AI returned an invalid response", code="gemini_generation_invalid_response") from exc
        try:
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            result = json.loads(text)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise GeminiError(invalid_message) from exc
        return result, body.get("usageMetadata") or {}

    async def _generate(self, parts: list[dict]) -> tuple[dict, dict]:
        candidate, usage = await self._generate_json(
            parts,
            CANDIDATE_SCHEMA,
            temperature=0.7,
            invalid_message="AI returned an invalid draft",
        )
        if not all(key in candidate for key in CANDIDATE_SCHEMA["required"]):
            raise GeminiError("AI returned an incomplete draft")
        return candidate, usage

    async def create_analytics_insights(self, evidence: dict) -> tuple[list[dict], dict]:
        prompt = (
            "You are Reverb's pragmatic social-performance analyst. Create 3 to 5 concise insight cards "
            "using only the supplied evidence. Reverb has already calculated every number: never calculate, "
            "estimate, invent, or imply causation. Prefer a specific next action over generic advice. Treat "
            "cross-platform exposure metrics as directional, not identical. Preserve the supplied confidence "
            "level. Select only supplied candidate IDs and do not repeat numbers in explanation or action; the "
            "interface renders Reverb's verified metric separately. If evidence is limited, say it is an early "
            "signal. Return only the requested schema. Evidence JSON: "
            f"{json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))}"
        )
        result, usage = await self._generate_json(
            [{"text": prompt}],
            ANALYTICS_INSIGHTS_SCHEMA,
            temperature=0.25,
            invalid_message="AI returned invalid analytics insights",
        )
        insights = result.get("insights")
        if not isinstance(insights, list) or not insights:
            raise GeminiError("AI returned incomplete analytics insights")
        return insights[:5], usage

    async def create_candidate(
        self,
        *,
        file: dict,
        current_caption: str,
        hashtags: list[str],
        platforms: list[str],
        generation_context: str = "",
    ) -> tuple[dict, dict | None, dict]:
        context_instruction = (
            f"Creator-provided additional context (use as guidance, preserve the facts, and do not invent details): "
            f"{generation_context!r}."
            if generation_context.strip()
            else "No additional creator context was provided."
        )
        prompt = (
            "First describe only what is directly visible or audible in this creator video. In video_observations, "
            "write one concise summary, up to six concrete details, and any important uncertainty. Do not infer "
            "identities, locations, products, outcomes, or claims that the video does not establish. Then create "
            "accurate, engaging social copy grounded in those observations and the creator's context. "
            f"Selected platforms: {', '.join(platforms)}. Existing caption: {current_caption!r}. "
            f"Existing hashtags: {', '.join(hashtags)}. {context_instruction} "
            "Keep each platform's conventions. Every caption and description field must contain body text only: "
            "do not embed hashtags in those fields. Put hashtags only in the hashtags array, without a leading # "
            "and without spaces. Keep YouTube title within 100 characters, Facebook title within 255, TikTok and "
            "Instagram captions within 2200, and YouTube description within 5000 UTF-8 bytes after hashtags are "
            "appended. Use an empty string for an unselected platform. Return only the schema."
        )
        result, usage = await self._generate_json(
            [
                {"fileData": {"mimeType": file.get("mimeType") or file.get("mime_type") or "video/mp4", "fileUri": file["uri"]}},
                {"text": prompt},
            ],
            VIDEO_GENERATION_SCHEMA,
            temperature=0.7,
            invalid_message="AI returned an invalid video draft",
        )
        if not all(key in result for key in CANDIDATE_SCHEMA["required"]):
            raise GeminiError("AI returned an incomplete video draft")
        try:
            observations = validate_video_observations(result.pop("video_observations", None))
        except GeminiError:
            # The copy is still useful. Older and malformed results can be
            # re-analyzed explicitly instead of failing the whole AI job.
            observations = None
        return result, observations, usage

    async def adjust_candidate(
        self,
        candidate: dict,
        adjustment: str,
        generation_context: str = "",
        *,
        video_observations: dict | None = None,
        platforms: list[str] | None = None,
    ) -> tuple[dict, dict]:
        context_instruction = (
            "Follow this creator-provided context. It may clarify or correct the observation notes; omit any "
            "contradicted note, and do not invent further details: "
            f"{generation_context!r}."
            if generation_context.strip()
            else "No additional creator context was provided."
        )
        observation_instruction = (
            "Ground every factual claim in these previously observed video details or explicit creator context. "
            f"Video observations: {json.dumps(video_observations, ensure_ascii=False)}. "
            if video_observations else "Do not add new factual claims not present in the previous copy or creator context. "
        )
        variation_instruction = (
            "Create a genuinely different version of the wording and opening, not just a small edit. "
            if adjustment == "regenerate" else f"Apply the '{adjustment}' adjustment. "
        )
        prompt = (
            f"Rewrite this social content. {variation_instruction}{observation_instruction}"
            f"Selected platforms: {', '.join(platforms or [])}. Use an empty string for unselected platforms. "
            "Preserve the facts and return every field. "
            "Keep hashtags only in the hashtags array without # characters or spaces; captions must contain body "
            "text only. Preserve all platform limits. "
            f"{context_instruction} Candidate JSON: {json.dumps(candidate, ensure_ascii=False)}"
        )
        return await self._generate([{"text": prompt}])
