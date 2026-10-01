import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.billing import activate_transaction, apply_billing_event
from app.config import get_settings
from app.database import SessionLocal
from app.models import AffiliateCommission, BillingEvent, ReferralCheckout, Subscription, User
from app.paystack import PaystackClient, PaystackError
from app.referral_billing import next_month, setup_referral_renewal


def account(client, email, code=None):
    response = client.post("/v1/auth/register", json={"name": "Creator", "email": email, "password": "password123", "referral_code": code})
    assert response.status_code == 201, response.text
    async def verified():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == email.casefold()))
            user.email_verified = True
            await db.commit()
            return user
    return asyncio.run(verified()), {"Authorization": f"Bearer {response.json()['access_token']}"}


def configure(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "affiliate_enabled", True)
    monkeypatch.setattr(settings, "paystack_basic_plan_code", "PLN_basic")
    monkeypatch.setattr(settings, "paystack_pro_plan_code", "PLN_pro")
    monkeypatch.setattr("app.tasks.sync_analytics.delay", lambda *_args: None)


def test_paystack_discount_is_not_overridden_by_a_plan(monkeypatch):
    requests = []
    async def fake_request(self, method, path, **kwargs):
        requests.append(kwargs["json"])
        return {}
    monkeypatch.setattr(PaystackClient, "_request", fake_request)
    async def run():
        client = PaystackClient()
        await client.initialize_checkout(email="test@example.com", plan_code="PLN_basic", plan="basic", user_id="user", amount_kobo=800_000, reference="test-ref")
        await client.initialize_checkout(email="test@example.com", plan_code="PLN_basic", plan="basic", user_id="user")
        await client.create_subscription("CUS_creator", "PLN_basic", "AUTH_creator", "2026-11-01T00:00:00+00:00")
    asyncio.run(run())
    assert requests[0]["amount"] == 800_000 and "plan" not in requests[0]
    assert requests[0]["channels"] == ["card"]
    assert requests[1]["plan"] == "PLN_basic" and "amount" not in requests[1]
    assert requests[2]["plan"] == "PLN_basic" and requests[2]["start_date"].startswith("2026-11-01")


@pytest.mark.parametrize("plan,charged,commission", [("basic", 800_000, 200_000), ("pro", 1_600_000, 400_000)])
@pytest.mark.parametrize("webhook_first", [True, False])
def test_discount_and_original_price_commission_are_once_only(client, monkeypatch, plan, charged, commission, webhook_first):
    configure(monkeypatch)
    suffix = f"{plan}-{webhook_first}"
    referrer, _ = account(client, f"discount-referrer-{suffix}@example.com")
    creator, headers = account(client, f"discount-creator-{suffix}@example.com", referrer.referral_code)
    payments, initializations, renewals = {}, [], []
    async def verify(self, reference):
        if reference not in payments:
            raise PaystackError("Transaction reference not found.", 400 if webhook_first else 404)
        return payments[reference]
    async def initialize(self, **kwargs):
        initializations.append(kwargs)
        return {"reference": kwargs["reference"], "authorization_url": "https://checkout.paystack.com/test"}
    async def listed(self, customer_id):
        assert customer_id == "123"
        return []
    async def renewal(self, customer, code, authorization, start):
        renewals.append((customer, code, authorization, start))
        return {"subscription_code": f"SUB_{suffix}", "email_token": "token"}
    monkeypatch.setattr(PaystackClient, "verify_transaction", verify)
    monkeypatch.setattr(PaystackClient, "initialize_checkout", initialize)
    monkeypatch.setattr(PaystackClient, "list_subscriptions", listed)
    monkeypatch.setattr(PaystackClient, "create_subscription", renewal)
    offer = client.get("/v1/billing/me", headers=headers).json()["referral_discount"]
    assert offer["first_payment_amounts"] == {"basic": 800_000, "pro": 1_600_000}
    opened = client.post("/v1/billing/checkout", headers=headers, json={"plan": plan})
    assert opened.status_code == 200, opened.text
    assert client.post("/v1/billing/checkout", headers=headers, json={"plan": plan}).json() == opened.json()
    assert len(initializations) == 1 and initializations[0]["amount_kobo"] == charged
    other = "pro" if plan == "basic" else "basic"
    assert client.post("/v1/billing/checkout", headers=headers, json={"plan": other}).status_code == 409
    reference = opened.json()["reference"]
    paid_at = datetime.now(UTC)
    payments[reference] = {
        "reference": reference, "status": "success", "amount": charged, "currency": "NGN",
        "plan": None, "plan_object": None,
        "paid_at": paid_at.isoformat(), "metadata": {"reverb_user_id": creator.id, "reverb_plan": plan},
        "customer": {"id": 123, "email": creator.email, "customer_code": "CUS_creator"},
        "authorization": {"authorization_code": "AUTH_creator", "reusable": True},
    }
    async def webhook():
        async with SessionLocal() as db:
            event = BillingEvent(provider_event_id=f"discount-{suffix}", event_type="charge.success", payload={"event": "charge.success", "data": payments[reference]})
            db.add(event)
            await db.commit()
            await apply_billing_event(db, event)
            await apply_billing_event(db, event)
    if webhook_first:
        asyncio.run(webhook())
    assert client.post("/v1/billing/verify", headers=headers, json={"reference": reference}).status_code == 200
    if not webhook_first:
        asyncio.run(webhook())
    assert client.post("/v1/billing/verify", headers=headers, json={"reference": reference}).status_code == 200
    assert client.get("/v1/billing/me", headers=headers).json()["referral_discount"] is None

    async def assertions():
        async with SessionLocal() as db:
            checkout = await db.get(ReferralCheckout, creator.id)
            earned = await db.scalar(select(AffiliateCommission).where(AffiliateCommission.referred_user_id == creator.id))
            assert earned.amount_kobo == commission
            assert earned.payment_amount_kobo == charged
            assert earned.commission_base_kobo == charged * 100 // 80
            assert (await db.get(Subscription, checkout.subscription_id)).amount_kobo == earned.commission_base_kobo
            assert checkout.renewal_start_at.replace(tzinfo=UTC) == next_month(paid_at)
            await setup_referral_renewal(db, creator.id)
            await setup_referral_renewal(db, creator.id)
            assert checkout.renewal_state == "complete"
            assert checkout.authorization_code is None
            renewal_event = BillingEvent(provider_event_id=f"renewal-{suffix}", event_type="charge.success", payload={"event": "charge.success", "data": {
                **payments[reference], "reference": f"renewal-{reference}", "amount": earned.commission_base_kobo,
                "subscription_code": f"SUB_{suffix}", "paid_at": next_month(paid_at).isoformat(),
            }})
            db.add(renewal_event)
            await db.commit()
            await apply_billing_event(db, renewal_event)
            assert (await db.get(Subscription, checkout.subscription_id)).paid_through.replace(tzinfo=UTC) == next_month(next_month(paid_at))
            duplicate = BillingEvent(provider_event_id=f"renewal-duplicate-{suffix}", event_type="charge.success", payload=renewal_event.payload)
            db.add(duplicate)
            await db.commit()
            await apply_billing_event(db, duplicate)
            assert (await db.get(Subscription, checkout.subscription_id)).paid_through.replace(tzinfo=UTC) == next_month(next_month(paid_at))
            assert await db.scalar(select(func.count(AffiliateCommission.id)).where(AffiliateCommission.referred_user_id == creator.id)) == 1
            refund = BillingEvent(provider_event_id=f"refund-{suffix}", event_type="refund.processed", payload={"event": "refund.processed", "data": {"transaction_reference": reference}})
            db.add(refund)
            await db.commit()
            await apply_billing_event(db, refund)
            assert earned.reversed_at is not None
    asyncio.run(assertions())
    assert len(renewals) == 1 and renewals[0][1] == f"PLN_{plan}"
    assert datetime.fromisoformat(renewals[0][3]) == next_month(paid_at)
    assert client.get("/v1/billing/me", headers=headers).json()["referral_discount"] is None


def test_unissued_discount_is_rejected_and_does_not_consume_first_payment(client, monkeypatch):
    configure(monkeypatch)
    creator, _ = account(client, "discount-tamper@example.com")
    async def run():
        async with SessionLocal() as db:
            user = await db.get(User, creator.id)
            with pytest.raises(Exception) as error:
                await activate_transaction(db, user, {"reference": "unissued-discount", "status": "success", "amount": 800_000, "currency": "NGN", "metadata": {"reverb_user_id": user.id, "reverb_plan": "basic"}, "customer": {"email": user.email}})
            assert error.value.status_code == 400
            assert user.first_paid_reference is None
    asyncio.run(run())


def test_calendar_month_handles_month_end_and_leap_year():
    assert next_month(datetime(2026, 1, 31, 12, tzinfo=UTC)) == datetime(2026, 2, 28, 12, tzinfo=UTC)
    assert next_month(datetime(2028, 1, 31, 12, tzinfo=UTC)) == datetime(2028, 2, 29, 12, tzinfo=UTC)
    assert next_month(datetime(2026, 12, 31, 12, tzinfo=UTC)) == datetime(2027, 1, 31, 12, tzinfo=UTC)


def test_uncertain_renewal_is_reconciled_not_created_twice_and_can_be_cancelled(client, monkeypatch):
    configure(monkeypatch)
    creator, headers = account(client, "uncertain-referral-renewal@example.com")
    calls, candidates, disabled = [], [], []
    start = next_month(datetime.now(UTC))
    async def seed():
        async with SessionLocal() as db:
            subscription = Subscription(user_id=creator.id, plan="basic", status="active", reference="uncertain-discount", paystack_customer_code="CUS_creator", amount_kobo=1_000_000, paid_through=start)
            db.add(subscription)
            await db.flush()
            db.add(ReferralCheckout(user_id=creator.id, reference="uncertain-discount", plan="basic", original_amount_kobo=1_000_000, amount_kobo=800_000, subscription_id=subscription.id, paid_at=datetime.now(UTC), customer_id="123", authorization_code="AUTH_creator", renewal_start_at=start, renewal_state="pending"))
            await db.commit()
    asyncio.run(seed())
    async def listed(self, customer_id):
        return candidates
    async def create(self, *args):
        calls.append(args)
        raise PaystackError("Paystack is temporarily unavailable", 503)
    async def disable(self, code, token):
        disabled.append((code, token))
        return {}
    monkeypatch.setattr(PaystackClient, "list_subscriptions", listed)
    monkeypatch.setattr(PaystackClient, "create_subscription", create)
    monkeypatch.setattr(PaystackClient, "disable_subscription", disable)
    async def run():
        async with SessionLocal() as db:
            await setup_referral_renewal(db, creator.id)
            await setup_referral_renewal(db, creator.id)
            assert (await db.get(ReferralCheckout, creator.id)).renewal_state == "uncertain"
    asyncio.run(run())
    assert len(calls) == 1
    assert client.post("/v1/billing/cancel", headers=headers).status_code == 200
    candidates.append({"plan": {"plan_code": "PLN_basic"}, "authorization": {"authorization_code": "AUTH_creator"}, "start": int(start.timestamp()), "subscription_code": "SUB_recovered", "email_token": "recovered-token"})
    async def recovered():
        async with SessionLocal() as db:
            await setup_referral_renewal(db, creator.id)
            checkout = await db.get(ReferralCheckout, creator.id)
            assert checkout.renewal_state == "cancelled"
            assert (await db.get(Subscription, checkout.subscription_id)).cancel_at_period_end
    asyncio.run(recovered())
    assert len(calls) == 1 and disabled == [("SUB_recovered", "recovered-token")]


def test_discount_reference_cannot_be_claimed_by_another_user(client, monkeypatch):
    configure(monkeypatch)
    owner, _ = account(client, "discount-reference-owner@example.com")
    other, _ = account(client, "discount-reference-other@example.com")
    async def run():
        async with SessionLocal() as db:
            db.add(ReferralCheckout(user_id=owner.id, reference="owned-referral-reference", plan="basic", original_amount_kobo=1_000_000, amount_kobo=800_000))
            await db.commit()
            with pytest.raises(Exception) as error:
                await activate_transaction(db, await db.get(User, other.id), {"reference": "owned-referral-reference", "status": "success", "amount": 800_000})
            assert error.value.status_code == 403
    asyncio.run(run())
