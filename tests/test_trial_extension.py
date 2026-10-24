import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import EntitlementAuditEvent, Subscription, User
from app.tasks import _send_trial_extension_emails

ADMIN = "extension-admin@example.com"
RUN = {"n": 0}


def register(client, email: str) -> tuple[dict, dict[str, str]]:
    response = client.post(
        "/v1/auth/register", json={"name": "Trial Creator", "email": email, "password": "creator123"}
    )
    assert response.status_code == 201, response.text
    auth = response.json()
    return auth, {"Authorization": f"Bearer {auth['access_token']}"}


async def set_trial(email: str, ends_in: timedelta | None, *, verified: bool = True, paid: bool = False) -> None:
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        user.email_verified = verified
        user.trial_ends_at = None if ends_in is None else datetime.now(UTC) + ends_in
        if paid:
            db.add(
                Subscription(
                    user_id=user.id, plan="basic", status="cancelled", reference=f"ext-{user.id}",
                    amount_kobo=1_000_000, currency="NGN",
                )
            )
        await db.commit()


async def load(email: str) -> User:
    async with SessionLocal() as db:
        return await db.scalar(select(User).where(User.email == email))


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def neutralise_existing_users() -> None:
    """The test database is shared, so mark users from earlier tests as already extended."""
    async with SessionLocal() as db:
        for user in await db.scalars(select(User)):
            user.trial_extended_at = user.trial_extended_at or datetime.now(UTC)
            user.trial_extension_emailed_at = user.trial_extension_emailed_at or datetime.now(UTC)
        await db.commit()


def setup(client, monkeypatch):
    RUN["n"] += 1
    monkeypatch.setattr(get_settings(), "admin_emails", f"admin-{RUN['n']}-{ADMIN}")
    monkeypatch.setattr("app.tasks.send_trial_extension_emails.delay", lambda: None)
    asyncio.run(neutralise_existing_users())
    _, headers = register(client, f"admin-{RUN['n']}-{ADMIN}")
    for email, ends, kwargs in [
        ("ext-active@example.com", timedelta(days=2), {}),
        ("ext-expired@example.com", timedelta(days=-5), {}),
        ("ext-paid@example.com", timedelta(days=-5), {"paid": True}),
        ("ext-unverified@example.com", None, {"verified": False}),
    ]:
        email = f"{RUN['n']}-{email}"
        register(client, email)
        asyncio.run(set_trial(email, ends, **kwargs))
    return headers


def call(client, headers, dry_run: bool):
    response = client.post("/v1/admin/trial-extension", headers=headers, json={"reason": "Launch week", "dry_run": dry_run})
    assert response.status_code == 200, response.text
    return response.json()


def test_dry_run_counts_without_changing_anyone(client, monkeypatch):
    headers = setup(client, monkeypatch)
    before = asyncio.run(load(f"{RUN['n']}-ext-active@example.com")).trial_ends_at

    body = call(client, headers, True)

    assert body["applied"] is False and body["extra_days"] == 7
    assert body["active"] == 1 and body["expired"] == 1 and body["total"] == 2
    assert asyncio.run(load(f"{RUN['n']}-ext-active@example.com")).trial_ends_at == before
    assert asyncio.run(load(f"{RUN['n']}-ext-active@example.com")).trial_extended_at is None


def test_extends_active_and_expired_trials_once_and_skips_paid_and_unverified(client, monkeypatch):
    headers = setup(client, monkeypatch)
    active_before = aware(asyncio.run(load(f"{RUN['n']}-ext-active@example.com")).trial_ends_at)
    paid_before = asyncio.run(load(f"{RUN['n']}-ext-paid@example.com")).trial_ends_at

    body = call(client, headers, False)
    assert body["applied"] is True and body["total"] == 2

    active = asyncio.run(load(f"{RUN['n']}-ext-active@example.com"))
    assert aware(active.trial_ends_at) == active_before + timedelta(days=7)
    expired = aware(asyncio.run(load(f"{RUN['n']}-ext-expired@example.com")).trial_ends_at)
    assert timedelta(days=6, hours=23) < expired - datetime.now(UTC) <= timedelta(days=7)
    assert asyncio.run(load(f"{RUN['n']}-ext-paid@example.com")).trial_ends_at == paid_before
    assert asyncio.run(load(f"{RUN['n']}-ext-unverified@example.com")).trial_ends_at is None

    again = call(client, headers, False)
    assert again["total"] == 0
    assert aware(asyncio.run(load(f"{RUN['n']}-ext-active@example.com")).trial_ends_at) == active_before + timedelta(days=7)

    async def audits() -> int:
        async with SessionLocal() as db:
            rows = await db.scalars(select(EntitlementAuditEvent).where(EntitlementAuditEvent.action == "trial_extended_bulk"))
            return len(list(rows))

    assert asyncio.run(audits()) == 2


def test_non_admin_cannot_extend_trials(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_emails", ADMIN)
    _, headers = register(client, "ext-not-admin@example.com")
    response = client.post("/v1/admin/trial-extension", headers=headers, json={"reason": "Launch week", "dry_run": False})
    assert response.status_code == 403


def test_emails_each_extended_creator_once_and_retries_failures(client, monkeypatch):
    headers = setup(client, monkeypatch)
    call(client, headers, False)
    sent: list[tuple[str, int]] = []
    fail = {"on": True}

    async def fake_send(email: str, name: str, extra_days: int, ends_at: str) -> None:
        if fail["on"]:
            raise RuntimeError("Brevo unavailable")
        sent.append((email, extra_days))

    monkeypatch.setattr("app.email.send_trial_extended_email", fake_send)
    asyncio.run(_send_trial_extension_emails())
    assert sent == []
    assert asyncio.run(load(f"{RUN['n']}-ext-active@example.com")).trial_extension_emailed_at is None

    fail["on"] = False
    asyncio.run(_send_trial_extension_emails())
    asyncio.run(_send_trial_extension_emails())
    assert sorted(sent) == [(f"{RUN['n']}-ext-active@example.com", 7), (f"{RUN['n']}-ext-expired@example.com", 7)]


def test_extension_emails_respect_the_kill_switch(client, monkeypatch):
    headers = setup(client, monkeypatch)
    call(client, headers, False)
    sent: list[str] = []

    async def fake_send(email: str, name: str, extra_days: int, ends_at: str) -> None:
        sent.append(email)

    monkeypatch.setattr("app.email.send_trial_extended_email", fake_send)
    monkeypatch.setattr(get_settings(), "trial_extension_emails_enabled", False)
    asyncio.run(_send_trial_extension_emails())
    assert sent == []
