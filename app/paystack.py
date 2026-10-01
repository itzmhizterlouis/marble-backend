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

    async def initialize_checkout(self, *, email: str, plan_code: str, plan: str, user_id: str, amount_kobo: int | None = None, reference: str | None = None) -> dict:
        payload = {
            "email": email,
            "callback_url": self.settings.paystack_callback_url,
            "metadata": {"reverb_user_id": user_id, "reverb_plan": plan},
        }
        if amount_kobo is None:
            payload["plan"] = plan_code
        else:
            # A plan overrides amount. Charge the discounted month separately,
            # then use its reusable authorization for full-price renewal.
            payload.update(amount=amount_kobo, currency="NGN", channels=["card"], reference=reference)
        return await self._request(
            "POST",
            "/transaction/initialize",
            json=payload,
        )

    async def create_subscription(self, customer: str, plan_code: str, authorization: str, start_date: str) -> dict:
        return await self._request("POST", "/subscription", json={
            "customer": customer, "plan": plan_code, "authorization": authorization, "start_date": start_date,
        })

    async def list_subscriptions(self, customer_id: str) -> list[dict]:
        return await self._request("GET", "/subscription", params={"customer": customer_id, "perPage": 100})

    async def verify_transaction(self, reference: str) -> dict:
        return await self._request("GET", f"/transaction/verify/{reference}")

    async def fetch_transaction(self, transaction_id: int) -> dict:
        return await self._request("GET", f"/transaction/{transaction_id}")

    async def disable_subscription(self, subscription_code: str, email_token: str) -> dict:
        return await self._request(
            "POST", "/subscription/disable", json={"code": subscription_code, "token": email_token}
        )

    async def manage_link(self, subscription_code: str) -> dict:
        return await self._request("GET", f"/subscription/{subscription_code}/manage/link")

    async def banks(self) -> list[dict]:
        return await self._request("GET", "/bank", params={"currency": "NGN", "perPage": 200})

    async def resolve_account(self, bank_code: str, account_number: str) -> dict:
        return await self._request("GET", "/bank/resolve", params={"bank_code": bank_code, "account_number": account_number})

    async def create_transfer_recipient(self, name: str, bank_code: str, account_number: str) -> dict:
        return await self._request("POST", "/transferrecipient", json={"type": "nuban", "name": name, "bank_code": bank_code, "account_number": account_number, "currency": "NGN"})

    async def initiate_transfer(self, reference: str, recipient: str, amount_kobo: int) -> dict:
        return await self._request("POST", "/transfer", json={"source": "balance", "amount": amount_kobo, "recipient": recipient, "reference": reference, "reason": "Reverb creator referral earnings", "currency": "NGN"})

    async def verify_transfer(self, reference: str) -> dict:
        return await self._request("GET", f"/transfer/verify/{reference}")

    async def finalize_transfer(self, transfer_code: str, otp: str) -> dict:
        return await self._request("POST", "/transfer/finalize_transfer", json={"transfer_code": transfer_code, "otp": otp})
