from __future__ import annotations

import httpx

from .config import get_settings


class PaystackError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class PaystackClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _headers(self) -> dict[str, str]:
        if not self.settings.paystack_secret_key:
            raise PaystackError("Paystack is not configured", 503)
        return {"Authorization": f"Bearer {self.settings.paystack_secret_key}"}

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient(base_url="https://api.paystack.co", timeout=30) as client:
                response = await client.request(method, path, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise PaystackError("Paystack is temporarily unavailable", 503) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise PaystackError("Paystack returned an invalid response") from exc
        if response.is_error or body.get("status") is False:
            raise PaystackError(str(body.get("message") or "Paystack request failed"), response.status_code)
        return body.get("data") or {}

    async def initialize_checkout(self, *, email: str, plan_code: str, plan: str, user_id: str) -> dict:
        return await self._request(
            "POST",
            "/transaction/initialize",
            json={
                "email": email,
                "plan": plan_code,
                "callback_url": self.settings.paystack_callback_url,
                "metadata": {"reverb_user_id": user_id, "reverb_plan": plan},
            },
        )

    async def verify_transaction(self, reference: str) -> dict:
        return await self._request("GET", f"/transaction/verify/{reference}")

    async def disable_subscription(self, subscription_code: str, email_token: str) -> dict:
        return await self._request(
            "POST", "/subscription/disable", json={"code": subscription_code, "token": email_token}
        )

    async def manage_link(self, subscription_code: str) -> dict:
        return await self._request("GET", f"/subscription/{subscription_code}/manage/link")
