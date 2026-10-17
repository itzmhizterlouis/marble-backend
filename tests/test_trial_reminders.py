import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Subscription, User
from app.tasks import _send_trial_reminders


def register(client, email: str) -> dict:
    response = client.post(
        "/v1/auth/register",
        json={"name": "Trial Creator", "email": email, "password": "creator123"},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def set_trial(email: str, ends_in: timedelta, *, paid: bool = False) -> None:
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        user.email_verified = True
        user.trial_ends_at = datetime.now(UTC) + ends_in
        if paid:
            db.add(
                Subscription(
                    user_id=user.id,
                    plan="basic",
                    status="active",
                    reference=f"paid-{user.id}",
                    amount_kobo=1_000_000,
                    currency="NGN",
                    paid_through=datetime.now(UTC) + timedelta(days=31),
                )
            )
        await db.commit()


def capture_emails(monkeypatch, *, fail: bool = False) -> list[str]:
    sent: list[str] = []

    async def fake_send(email: str, name: str, ends_at: str) -> None:
        if fail:
            raise RuntimeError("Brevo unavailable")
        sent.append(email)

    monkeypatch.setattr("app.email.send_trial_ending_email", fake_send)
    return sent


def enforce(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "billing_enforcement_enabled", True)


def test_reminds_once_within_final_day_only(client, monkeypatch):
    enforce(monkeypatch)
    sent = capture_emails(monkeypatch)
    register(client, "ends-soon@example.com")
    register(client, "ends-later@example.com")
    register(client, "already-ended@example.com")
    asyncio.run(set_trial("ends-soon@example.com", timedelta(hours=20)))
    asyncio.run(set_trial("ends-later@example.com", timedelta(days=3)))
    asyncio.run(set_trial("already-ended@example.com", timedelta(hours=-1)))

    asyncio.run(_send_trial_reminders())
    asyncio.run(_send_trial_reminders())

    assert sent == ["ends-soon@example.com"]


def test_paid_creators_are_not_reminded(client, monkeypatch):
    enforce(monkeypatch)
    sent = capture_emails(monkeypatch)
    register(client, "paid-during-trial@example.com")
    asyncio.run(set_trial("paid-during-trial@example.com", timedelta(hours=5), paid=True))

    asyncio.run(_send_trial_reminders())

    assert sent == []


def test_restarted_trial_earns_a_new_reminder(client, monkeypatch):
    enforce(monkeypatch)
    sent = capture_emails(monkeypatch)
    register(client, "restarted@example.com")
    asyncio.run(set_trial("restarted@example.com", timedelta(hours=10)))
    asyncio.run(_send_trial_reminders())

    asyncio.run(set_trial("restarted@example.com", timedelta(hours=12)))
    asyncio.run(_send_trial_reminders())

    assert sent == ["restarted@example.com", "restarted@example.com"]


def test_failed_delivery_is_retried_next_run(client, monkeypatch):
    enforce(monkeypatch)
    capture_emails(monkeypatch, fail=True)
    register(client, "brevo-down@example.com")
    asyncio.run(set_trial("brevo-down@example.com", timedelta(hours=8)))
    asyncio.run(_send_trial_reminders())

    sent = capture_emails(monkeypatch)
    asyncio.run(_send_trial_reminders())

    assert sent == ["brevo-down@example.com"]


def test_kill_switch_pauses_reminders(client, monkeypatch):
    enforce(monkeypatch)
    monkeypatch.setattr(get_settings(), "trial_reminder_emails_enabled", False)
    sent = capture_emails(monkeypatch)
    register(client, "paused@example.com")
    asyncio.run(set_trial("paused@example.com", timedelta(hours=8)))

    asyncio.run(_send_trial_reminders())

    assert sent == []
