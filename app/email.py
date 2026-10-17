import logging
from html import escape

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)


async def send_transactional_email(
    to_email: str, to_name: str, subject: str, html: str
) -> str | None:
    settings = get_settings()
    logo_url = escape(f"{settings.frontend_url.rstrip('/')}/assets/reverb-logo-v2.jpg", quote=True)
    html = (
        f'<p><img src="{logo_url}" alt="Reverb" width="56" height="56" '
        'style="display:block;border:0;border-radius:12px" /></p>' + html
    )
    if not settings.brevo_api_key or not settings.brevo_sender_email:
        if settings.is_production:
            raise RuntimeError("Brevo is not configured")
        logger.info("Development email to %s: %s\n%s", to_email, subject, html)
        return None
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": settings.brevo_api_key, "accept": "application/json"},
            json={
                "sender": {"email": settings.brevo_sender_email, "name": settings.brevo_sender_name},
                "to": [{"email": to_email, "name": to_name}],
                "subject": subject,
                "htmlContent": html,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload.get("messageId")) if payload.get("messageId") else None


async def send_verification_email(email: str, name: str, token: str) -> None:
    url = f"{get_settings().frontend_url}/verify-email?token={token}"
    await send_transactional_email(
        email,
        name,
        "Verify your Reverb account",
        f'<p>Hi {escape(name)},</p><p>Verify your Reverb account to start publishing.</p><p><a href="{url}">Verify email</a></p>',
    )


async def send_password_reset_email(email: str, name: str, token: str) -> None:
    url = f"{get_settings().frontend_url}/reset-password?token={token}"
    await send_transactional_email(
        email,
        name,
        "Reset your Reverb password",
        f'<p>Hi {escape(name)},</p><p>This link expires in one hour.</p><p><a href="{url}">Reset password</a></p>',
    )


async def send_payment_attention_email(email: str, name: str, grace_until: str) -> None:
    url = f"{get_settings().frontend_url}/billing"
    await send_transactional_email(
        email,
        name,
        "Your Reverb payment needs attention",
        f'<p>Hi {escape(name)},</p><p>Your subscription renewal did not complete. Your paid features remain available until {escape(grace_until)}.</p><p>Paystack does not automatically retry this payment, so please update your payment method or subscribe again.</p><p><a href="{url}">Review billing</a></p>',
    )


async def send_trial_ending_email(email: str, name: str, ends_at: str) -> None:
    url = f"{get_settings().frontend_url}/pricing"
    await send_transactional_email(
        email,
        name,
        "Your Reverb Pro trial ends tomorrow",
        f'<p>Hi {escape(name)},</p><p>Your free Reverb Pro trial ends on {escape(ends_at)}.</p><p>Choose Basic or Pro to keep publishing. Nothing is charged automatically, and your posts and history stay in your account. Any posts scheduled after your trial ends will return to drafts unless you choose a plan.</p><p><a href="{url}">Choose a plan</a></p>',
    )


async def send_access_expired_email(email: str, name: str, cancelled_schedules: int) -> None:
    url = f"{get_settings().frontend_url}/billing"
    schedule_note = (
        f" {cancelled_schedules} future scheduled post{'s were' if cancelled_schedules != 1 else ' was'} returned to drafts."
        if cancelled_schedules
        else ""
    )
    await send_transactional_email(
        email,
        name,
        "Your Reverb paid access has ended",
        f'<p>Hi {escape(name)},</p><p>Your paid Reverb access has ended.{escape(schedule_note)}</p><p>Your history and account settings remain available.</p><p><a href="{url}">Choose a plan</a></p>',
    )
