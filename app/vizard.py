"""Vizard's public clipping contract; no provider secrets or messages reach creators."""
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import get_settings

API_BASE = "https://elb-api.vizard.ai/hvizard-server-front/open-api/v1"


class VizardError(Exception):
    def __init__(self, message="Video processing is temporarily unavailable", *, uncertain=False, retryable=False):
        super().__init__(message)
        self.uncertain = uncertain
        self.retryable = retryable


class VizardClient:
    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        submitting = method == "POST"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(100, connect=10)) as client:
                response = await client.request(method, f"{API_BASE}/{path}", json=body,
                                                headers={"VIZARDAI_API_KEY": get_settings().vizard_api_key})
            if response.status_code >= 500:
                raise VizardError(uncertain=submitting, retryable=not submitting)
            if response.status_code == 429:
                raise VizardError("Video processing is busy. Please try again later.", retryable=True)
            if response.status_code >= 400:
                raise VizardError("Vizard could not accept this request")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Invalid response")
        except (httpx.HTTPError, ValueError) as exc:
            raise VizardError(uncertain=submitting, retryable=not submitting) from exc
        code = result.get("code")
        if code not in {1000, 2000}:
            messages = {
                4001: "Video processing is not configured correctly. Please contact Reverb.",
                4003: "Video processing is busy. Please try again later.",
                4007: "Clipping capacity is exhausted. Please contact Reverb.",
                4008: "Vizard could not read this video. Please contact Reverb.",
                4010: "The video's spoken language could not be detected.",
            }
            raise VizardError(messages.get(code, "Vizard could not process this video"), retryable=code == 4003)
        return result

    async def submit(self, source_url: str, options: dict, name: str, extension: str) -> str:
        settings = get_settings()
        style = options.get("style", "clean")
        body = {
            "videoUrl": source_url, "videoType": 1, "ext": extension, "lang": "auto",
            "projectName": name, "preferLength": [3 if options.get("length") == "medium" else 2],
            "ratioOfClip": 1, "maxClipNumber": 5, "clipModel": options.get("model", settings.vizard_clip_model),
            "subtitleSwitch": int(options.get("subtitles", True)),
            "removeSilenceSwitch": int(options.get("remove_silence", False)),
            "headlineSwitch": 1, "highlightSwitch": int(style == "bold"), "emojiSwitch": 0,
            "autoBrollSwitch": 0,
        }
        template = settings.vizard_bold_template_id if style == "bold" else settings.vizard_clean_template_id
        if template:
            body["templateId"] = template
        if options.get("keywords", "").strip():
            body["keywords"] = options["keywords"].strip()
        result = await self._request("POST", "project/create", body)
        project_id = str(result.get("projectId") or "")
        if not project_id.isdigit() or len(project_id) > 64:
            raise VizardError("The submission needs an owner review before retrying", uncertain=True)
        return project_id

    async def status(self, project_id: str) -> dict:
        if not project_id.isdigit() or len(project_id) > 64:
            raise VizardError("Invalid clipping project")
        return await self._request("GET", f"project/query/{project_id}")

    async def download(self, url: str, destination: Path, maximum_bytes: int) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}
                or not host.endswith(".vizard.ai")):
            raise VizardError("The clip download address could not be verified")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15), follow_redirects=False) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise VizardError("This clip could not be downloaded yet", retryable=True)
                    size = 0
                    with destination.open("wb") as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > maximum_bytes:
                                raise VizardError("This generated clip exceeds Reverb's file limit")
                            output.write(chunk)
                    if size == 0:
                        raise VizardError("The generated clip was empty")
        except (httpx.HTTPError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise VizardError("The clip could not be saved yet", retryable=True) from exc
        except VizardError:
            destination.unlink(missing_ok=True)
            raise
