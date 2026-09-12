from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from .config import get_settings


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def upload_request_id(post_id: str, revision: int) -> str:
    """Return the stable provider request ID for one logical submission."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"marble:{post_id}:{revision}"))


def upload_idempotency_key(post_id: str, revision: int) -> str:
    return f"marble:{post_id}:{revision}"


class UploadPostClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _headers(self, **extra: str) -> dict[str, str]:
        if not self.settings.upload_post_api_key:
            raise ProviderError("provider_not_configured", "Upload-Post API key is not configured", 503)
        return {"Authorization": f"Apikey {self.settings.upload_post_api_key}", **extra}

    async def _json(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient(base_url=self.settings.upload_post_base_url, timeout=40) as client:
                response = await client.request(
                    method, path, headers=self._headers(**kwargs.pop("headers", {})), **kwargs
                )
        except httpx.HTTPError as exc:
            raise ProviderError("provider_unavailable", "Upload-Post is temporarily unavailable", 503) from exc
        if response.is_error:
            try:
                body = response.json()
                message = body.get("message") or body.get("error") or response.text
                code = body.get("error_code") or f"provider_http_{response.status_code}"
            except ValueError:
                message, code = response.text, f"provider_http_{response.status_code}"
            raise ProviderError(code, message, response.status_code)
        return response.json()

    async def verify_account(self) -> dict:
        return await self._json("GET", "/uploadposts/me")

    async def create_profile(self, username: str) -> dict:
        return await self._json("POST", "/uploadposts/users", json={"username": username})

    async def get_profile(self, username: str) -> dict:
        return await self._json("GET", f"/uploadposts/users/{username}")

    async def connection_access_url(self, username: str, platform: str, redirect_url: str) -> str:
        generated = await self._json(
            "POST",
            "/uploadposts/users/generate-jwt",
            json={
                "username": username,
                "redirect_url": redirect_url,
                "platforms": [platform],
                "show_calendar": False,
                "logo_image": f"{self.settings.frontend_url}/assets/reverb-logo.png",
                "redirect_button_text": "Return to Reverb",
                "connect_title": f"Connect {platform.title()} to Reverb",
                "connect_description": (
                    "Reverb uses Upload-Post to securely manage this publishing connection. "
                    "Authorize only the account you want Reverb to publish to."
                ),
            },
        )
        access_url = generated.get("access_url")
        if not access_url:
            raise ProviderError("provider_invalid_response", "Upload-Post did not return a connection URL")
        return access_url

    async def connection_url(self, username: str, platform: str, redirect_url: str) -> str:
        access_url = await self.connection_access_url(username, platform, redirect_url)
        token = parse_qs(urlparse(access_url).query).get("token", [None])[0]
        if not token:
            return access_url
        try:
            async with httpx.AsyncClient(base_url=self.settings.upload_post_base_url, timeout=40) as client:
                response = await client.post(
                    f"/uploadposts/oauth/{platform}/start",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"redirect_url": redirect_url},
                )
        except httpx.HTTPError as exc:
            raise ProviderError("provider_unavailable", "Upload-Post is temporarily unavailable", 503) from exc
        if response.is_success and response.json().get("authorize_url"):
            return response.json()["authorize_url"]
        return access_url

    async def facebook_pages(self, username: str) -> list[dict]:
        body = await self._json("GET", "/uploadposts/facebook/pages", params={"profile": username})
        return body.get("pages", [])

    @staticmethod
    def _generic_upload_title(versions: list[dict], youtube_title: str) -> str:
        """Return a safe Upload-Post fallback title for mixed-platform uploads."""
        if len(versions) == 1:
            return youtube_title
        for version in versions:
            if version.get("platform") == "youtube":
                continue
            caption = str(version.get("caption") or "").strip()
            if caption:
                return caption[:100]
        return youtube_title

    async def publish_video(
        self,
        *,
        profile: str,
        video_path: Path,
        post_id: str,
        revision: int,
        versions: list[dict],
        scheduled_at: str | None,
        timezone: str | None,
        facebook_page_id: str | None,
        request_id: str | None = None,
    ) -> dict:
        platforms = [version["platform"] for version in versions]
        youtube = next((item for item in versions if item["platform"] == "youtube"), None)
        youtube_title = str((youtube or {}).get("title") or "").strip()
        if youtube and not youtube_title:
            raise ProviderError("youtube_title_required", "YouTube requires a title", 422)
        data: dict[str, str | list[str]] = {
            "user": profile,
            "external_id": post_id,
            "async_upload": "true",
            "platform[]": platforms,
        }
        if youtube:
            # Upload-Post requires a generic title when YouTube is selected,
            # but that field is a fallback for every destination. In a mixed
            # upload, use a non-YouTube caption as the fallback so the explicit
            # YouTube title cannot become another platform's caption. The
            # platform-specific fields below remain authoritative.
            data["title"] = self._generic_upload_title(versions, youtube_title)
        request_id = request_id or upload_request_id(post_id, revision)
        data["request_id"] = request_id
        for version in versions:
            platform = version["platform"]
            if platform == "youtube":
                data["youtube_title"] = youtube_title
                data["youtube_description"] = version["caption"]
            elif platform == "facebook":
                data["facebook_title"] = version["caption"]
                data["facebook_description"] = version["caption"]
                data["facebook_media_type"] = "REELS"
            else:
                data[f"{platform}_title"] = version["caption"]
        if facebook_page_id:
            data["facebook_page_id"] = facebook_page_id
        if scheduled_at:
            data["scheduled_date"] = scheduled_at
            if timezone:
                data["timezone"] = timezone
        headers = self._headers(
            **{
                "Idempotency-Key": upload_idempotency_key(post_id, revision),
                "X-Request-Id": request_id,
            }
        )
        try:
            async with httpx.AsyncClient(base_url=self.settings.upload_post_base_url, timeout=None) as client:
                with video_path.open("rb") as video:
                    mime_type = "video/quicktime" if video_path.suffix.lower() == ".mov" else "video/mp4"
                    response = await client.post(
                        "/upload",
                        headers=headers,
                        data=data,
                        files={"video": (video_path.name, video, mime_type)},
                    )
        except (httpx.HTTPError, OSError) as exc:
            raise ProviderError("provider_unavailable", "Upload-Post is temporarily unavailable", 503) from exc
        if response.is_error:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise ProviderError(
                body.get("error_code", "publish_failed"),
                body.get("message") or body.get("error") or response.text,
                response.status_code,
            )
        return response.json()

    async def status(self, *, request_id: str | None = None, job_id: str | None = None) -> dict:
        params = {"request_id": request_id} if request_id else {"job_id": job_id}
        return await self._json("GET", "/uploadposts/status", params=params)

    async def retry(
        self,
        *,
        request_id: str | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        payload = {"request_id": request_id} if request_id else {"job_id": job_id}
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return await self._json(
            "POST", "/uploadposts/posts/retry", json=payload, headers=headers
        )

    async def update_schedule(self, job_id: str, payload: dict) -> dict:
        return await self._json("PATCH", f"/uploadposts/schedule/{job_id}", json=payload)

    async def cancel_schedule(self, job_id: str) -> dict:
        return await self._json("DELETE", f"/uploadposts/schedule/{job_id}")


def verify_upload_post_signature(
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    *,
    tolerance_seconds: int = 300,
) -> bool:
    secret = get_settings().upload_post_webhook_secret
    if not secret or not signature or not timestamp:
        return False
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - signed_at) > tolerance_seconds:
        return False
    signed_payload = timestamp.encode() + b"." + body
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    supplied = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, supplied)
