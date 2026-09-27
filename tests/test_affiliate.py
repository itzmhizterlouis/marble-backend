import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.affiliate import available_balance, payout_quote
from app.billing import activate_transaction, apply_billing_event
from app.config import get_settings
from app.database import SessionLocal
from app.models import AffiliateCommission, AffiliateLedgerEntry, AffiliatePayout, BillingEvent, User
from app.paystack import PaystackClient, PaystackError
from app.security import create_oauth_state, decode_oauth_state


def signup(client, email: str, referral_code: str | None = None) -> dict:
    response = client.post("/v1/auth/register", json={"name": "Creator", "email": email, "password": "password123", "referral_code": referral_code})
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def verify(email: str) -> User:
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        user.email_verified = True
        await db.commit()
        return user


def payment(user_id: str, email: str, reference: str, amount: int = 1_000_000, plan: str = "basic") -> dict:
    return {"status": "success", "reference": reference, "amount": amount, "currency": "NGN", "metadata": {"reverb_user_id": user_id, "reverb_plan": plan}, "customer": {"email": email}}


def test_google_state_and_fee_boundary():
    state = create_oauth_state("browser-nonce", "ABC123")
    assert decode_oauth_state(state)["referral_code"] == "ABC123"
    for amount in (200_000, 501_100, 1_000_000, 5_001_100):
        quote = payout_quote(amount)
        assert quote["requested_kobo"] <= amount
        assert quote["net_kobo"] + quote["fee_kobo"] == quote["requested_kobo"]
        assert quote["net_kobo"] >= 5_000


def test_signup_referral_is_bound_once_and_first_payment_is_idempotent(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "affiliate_enabled", True)
    referrer_headers = signup(client, "referrer-affiliate@example.com")
    referrer = asyncio.run(verify("referrer-affiliate@example.com"))
    code = client.get("/v1/referrals/me", headers=referrer_headers).json()["code"]
    assert client.get(f"/v1/referrals/validate?code={code}").json() == {"valid": True}
    assert client.post("/v1/auth/register", json={"name": "Bad", "email": "invalid-affiliate@example.com", "password": "password123", "referral_code": "WRONGCODE"}).status_code == 400
    signup(client, "referred-affiliate@example.com", code.lower())
    referred = asyncio.run(verify("referred-affiliate@example.com"))
    assert referred.referred_by_user_id == referrer.id

    async def run_payment():
        async with SessionLocal() as db:
            user = await db.get(User, referred.id)
            await activate_transaction(db, user, payment(user.id, user.email, "first-affiliate-payment"))
            await activate_transaction(db, user, payment(user.id, user.email, "first-affiliate-payment"))
            await activate_transaction(db, user, payment(user.id, user.email, "second-affiliate-payment"))
            commissions = (await db.scalars(select(AffiliateCommission).where(AffiliateCommission.referred_user_id == user.id))).all()
            assert len(commissions) == 1
            assert commissions[0].amount_kobo == 200_000
            assert commissions[0].payment_reference == "first-affiliate-payment"
            assert commissions[0].available_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(days=6)
            assert await available_balance(db, referrer.id) == 0

    async def no_disable(*_args):
        return {}

    monkeypatch.setattr(PaystackClient, "disable_subscription", no_disable)
    asyncio.run(run_payment())
    summary = client.get("/v1/referrals/me", headers=referrer_headers).json()
    assert summary["referral_count"] == 1
    assert summary["pending_kobo"] == 200_000
    assert "email" not in summary["commissions"][0]


def test_first_pro_payment_pays_four_thousand_and_earlier_reversal_cannot_reaward(client, monkeypatch):
    from app.tasks import sync_analytics

    monkeypatch.setattr(get_settings(), "affiliate_enabled", True)
    monkeypatch.setattr(sync_analytics, "delay", lambda *_args: None)
    signup(client, "pro-referrer@example.com")
    referrer = asyncio.run(verify("pro-referrer@example.com"))
    signup(client, "pro-referred@example.com", referrer.referral_code)
    referred = asyncio.run(verify("pro-referred@example.com"))

    async def run():
        from app.affiliate import reverse_commission

        async with SessionLocal() as db:
            await activate_transaction(db, await db.get(User, referred.id), payment(referred.id, referred.email, "first-pro-payment", amount=2_000_000, plan="pro"))
            commission = await db.scalar(select(AffiliateCommission).where(AffiliateCommission.referred_user_id == referred.id))
            assert commission.amount_kobo == 400_000
            await reverse_commission(db, "first-pro-payment", "refund.processed")
            await db.commit()
            await activate_transaction(db, await db.get(User, referred.id), payment(referred.id, referred.email, "another-pro-payment", amount=2_000_000, plan="pro"))
            assert (await db.scalars(select(AffiliateCommission).where(AffiliateCommission.referred_user_id == referred.id))).all() == [commission]

    async def no_disable(*_args):
        return {}

    monkeypatch.setattr(PaystackClient, "disable_subscription", no_disable)
    asyncio.run(run())


def test_bank_payout_reservation_transfer_and_refund(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "affiliate_enabled", True)
    monkeypatch.setattr(settings, "admin_emails", "owner-affiliate@example.com")
    owner_headers = signup(client, "owner-affiliate@example.com")
    creator_headers = signup(client, "payee-affiliate@example.com")
    creator = asyncio.run(verify("payee-affiliate@example.com"))
    signup(client, "payer-affiliate@example.com", creator.referral_code)
    referred = asyncio.run(verify("payer-affiliate@example.com"))

    async def make_earned():
        async with SessionLocal() as db:
            await activate_transaction(db, await db.get(User, referred.id), payment(referred.id, referred.email, "payout-affiliate-payment"))
            commission = await db.scalar(select(AffiliateCommission).where(AffiliateCommission.referred_user_id == referred.id))
            commission.available_at = datetime.now(UTC) - timedelta(days=1)
            entry = await db.scalar(select(AffiliateLedgerEntry).where(AffiliateLedgerEntry.commission_id == commission.id))
            entry.available_at = commission.available_at
            await db.commit()

    asyncio.run(make_earned())

    async def fake_banks(self):
        return [{"name": "Test Bank", "code": "123", "active": True}]

    async def fake_resolve(self, bank_code, account_number):
        return {"account_name": "CREATOR ACCOUNT"}

    async def fake_recipient(self, name, bank_code, account_number):
        return {"recipient_code": "RCP_test"}

    async def fake_verify(self, reference):
        raise PaystackError("Not found", 404)

    async def fake_transfer(self, reference, recipient, amount_kobo):
        assert recipient == "RCP_test"
        assert amount_kobo == 199_000
        return {"reference": reference, "status": "success", "transfer_code": "TRF_test"}

    monkeypatch.setattr(PaystackClient, "banks", fake_banks)
    monkeypatch.setattr(PaystackClient, "resolve_account", fake_resolve)
    monkeypatch.setattr(PaystackClient, "create_transfer_recipient", fake_recipient)
    monkeypatch.setattr(PaystackClient, "verify_transfer", fake_verify)
    monkeypatch.setattr(PaystackClient, "initiate_transfer", fake_transfer)
    resolved = client.post("/v1/referrals/bank/resolve", headers=creator_headers, json={"bank_code": "123", "account_number": "0123456789"})
    assert resolved.json()["account_name"] == "CREATOR ACCOUNT"
    bank = client.post("/v1/referrals/bank", headers=creator_headers, json={"bank_code": "123", "account_number": "0123456789", "account_name": "CREATOR ACCOUNT"})
    assert bank.status_code == 200, bank.text
    assert bank.json()["account_last_four"] == "6789"
    assert "account_number" not in bank.json()
    quote = client.post("/v1/referrals/payouts/quote", headers=creator_headers, json={"amount_kobo": 200_000}).json()
    assert quote["net_kobo"] == 199_000
    headers = {**creator_headers, "Idempotency-Key": "affiliate-payout-key-1"}
    payload = {"amount_kobo": 200_000, "recipient_id": quote["recipient_id"], "quoted_fee_kobo": quote["fee_kobo"], "quoted_net_kobo": quote["net_kobo"]}
    assert client.post("/v1/referrals/payouts", headers=headers, json={**payload, "recipient_id": "different-bank"}).status_code == 409
    requested = client.post("/v1/referrals/payouts", headers=headers, json=payload)
    assert requested.status_code == 201, requested.text
    assert client.post("/v1/referrals/payouts", headers=headers, json=payload).json()["id"] == requested.json()["id"]
    assert client.post("/v1/referrals/payouts", headers={**creator_headers, "Idempotency-Key": "affiliate-payout-key-2"}, json=payload).status_code == 409
    payout_id = requested.json()["id"]
    assert client.get("/v1/admin/referrals/payouts", headers=creator_headers).status_code == 403
    approved = client.post(f"/v1/admin/referrals/payouts/{payout_id}/approve", headers=owner_headers, json={"reason": "Verified referral and bank"})
    assert approved.status_code == 200, approved.text
    # The initiation response alone cannot confirm that money reached the bank.
    assert approved.json()["status"] == "pending"

    async def finish_and_reverse():
        async with SessionLocal() as db:
            payout = await db.get(AffiliatePayout, payout_id)
            db.add(BillingEvent(provider_event_id="affiliate-transfer-success", event_type="transfer.success", payload={"event": "transfer.success", "data": {"reference": payout.transfer_reference}}))
            await db.flush()
            event = await db.scalar(select(BillingEvent).where(BillingEvent.provider_event_id == "affiliate-transfer-success"))
            await apply_billing_event(db, event)
            assert payout.status == "paid"
            db.add(BillingEvent(provider_event_id="affiliate-refund", event_type="refund.processed", payload={"event": "refund.processed", "data": {"transaction_reference": "payout-affiliate-payment"}}))
            await db.flush()
            event = await db.scalar(select(BillingEvent).where(BillingEvent.provider_event_id == "affiliate-refund"))
            await apply_billing_event(db, event)
            assert await available_balance(db, creator.id) == -200_000

    asyncio.run(finish_and_reverse())
    assert client.get("/v1/referrals/me", headers=creator_headers).json()["available_kobo"] == -200_000
