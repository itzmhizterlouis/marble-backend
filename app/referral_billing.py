from __future__ import annotations

import calendar
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .events import publish_realtime_event
from .models import ReferralCheckout, Subscription, User, utcnow
from .paystack import PaystackClient, PaystackError
from .pricing import PLAN_AMOUNTS


def next_month(value: datetime) -> datetime:
    month = value.month % 12 + 1
    year = value.year + (value.month == 12)
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


async def lock_billing_user(db: AsyncSession, user_id: str) -> User:
    return await db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))


async def initialize_referral_checkout(db: AsyncSession, user: User, plan: str, plan_code: str) -> dict:
    checkout = await db.get(ReferralCheckout, user.id)
    if checkout and checkout.plan != plan:
        raise HTTPException(status_code=409, detail={
            "code": "referral_checkout_in_progress",
            "message": f"Your discounted {checkout.plan.title()} checkout is already open. Resume it to avoid a duplicate payment.",
        })
    if not checkout:
        checkout = ReferralCheckout(
            user_id=user.id, reference=f"rvbr-{uuid.uuid4().hex}", plan=plan,
            original_amount_kobo=PLAN_AMOUNTS[plan], amount_kobo=PLAN_AMOUNTS[plan] * 80 // 100,
        )
        db.add(checkout)
        # Persist the reference before contacting Paystack, including when the
        # process exits after Paystack accepted the request but before replying.
        await db.commit()
    await lock_billing_user(db, user.id)
    await db.refresh(checkout)
    if checkout.authorization_url:
        return {"authorization_url": checkout.authorization_url, "reference": checkout.reference}
    client = PaystackClient()
    try:
        previous = await client.verify_transaction(checkout.reference)
    except PaystackError as exc:
        # Paystack uses 400, not 404, for an unknown transaction reference.
        not_found = exc.status_code == 404 or (exc.status_code == 400 and str(exc).casefold().rstrip(".") == "transaction reference not found")
        if not not_found:
            raise
    else:
        if previous.get("status") == "success":
            from .billing import activate_transaction

            await activate_transaction(db, user, previous)
            raise HTTPException(status_code=409, detail={"code": "payment_already_completed", "message": "Your payment is already complete. Refresh your billing page."})
        # Verify does not return the hosted checkout URL. Do not mint another
        # reference when the first initialization's outcome is uncertain.
        raise HTTPException(status_code=409, detail={"code": "checkout_needs_recovery", "message": "Your checkout needs recovery. Contact hello@reverb.com.ng before paying again."})
    data = await client.initialize_checkout(
        email=user.email, plan_code=plan_code, plan=plan, user_id=user.id,
        amount_kobo=checkout.amount_kobo, reference=checkout.reference,
    )
    if data.get("reference") != checkout.reference or not data.get("authorization_url"):
        raise PaystackError("Paystack did not confirm your checkout reference")
    checkout.authorization_url = data["authorization_url"]
    await db.commit()
    return {"authorization_url": checkout.authorization_url, "reference": checkout.reference}


async def setup_referral_renewal(db: AsyncSession, user_id: str) -> None:
    # Also serializes cancellation against provider subscription creation.
    await lock_billing_user(db, user_id)
    checkout = await db.get(ReferralCheckout, user_id)
    if not checkout or not checkout.paid_at or not checkout.subscription_id:
        return
    subscription = await db.get(Subscription, checkout.subscription_id)
    if not subscription or checkout.renewal_state in {"complete", "cancelled", "attention"}:
        return
    if subscription.cancel_at_period_end and checkout.renewal_state == "pending":
        checkout.renewal_state = "cancelled"
        await db.commit()
        return
    if not checkout.customer_id or not checkout.authorization_code or not checkout.renewal_start_at:
        checkout.renewal_state = "attention"
        await db.commit()
        return
    settings = get_settings()
    plan_code = settings.paystack_basic_plan_code if checkout.plan == "basic" else settings.paystack_pro_plan_code
    if not plan_code:
        return
    client = PaystackClient()
    candidates = await client.list_subscriptions(checkout.customer_id)
    start = checkout.renewal_start_at.replace(tzinfo=UTC) if checkout.renewal_start_at.tzinfo is None else checkout.renewal_start_at
    match = next((item for item in candidates if (
        isinstance(item.get("plan"), dict) and item["plan"].get("plan_code") == plan_code
        and isinstance(item.get("authorization"), dict) and item["authorization"].get("authorization_code") == checkout.authorization_code
        and str(item.get("start")) == str(int(start.timestamp()))
    )), None)
    if not match and checkout.renewal_state in {"creating", "uncertain"}:
        # A timed-out POST may already have created a subscription. Only
        # reconcile it (or accept its signed webhook); never POST a second one.
        checkout.renewal_state = "uncertain"
        await db.commit()
        return
    if not match:
        checkout.renewal_state = "creating"
        checkout.renewal_attempted_at = utcnow()
        await db.commit()
        await lock_billing_user(db, user_id)
        await db.refresh(subscription)
        if subscription.cancel_at_period_end:
            checkout.renewal_state = "cancelled"
            await db.commit()
            return
        try:
            match = await client.create_subscription(subscription.paystack_customer_code, plan_code, checkout.authorization_code, start.isoformat())
        except PaystackError as exc:
            checkout.renewal_state = "attention" if 400 <= exc.status_code < 500 and exc.status_code not in {408, 429} else "uncertain"
            await db.commit()
            await publish_realtime_event(user_id, "billing.updated")
            return
    if not match.get("subscription_code") or not match.get("email_token"):
        checkout.renewal_state = "uncertain"
        await db.commit()
        return
    subscription.paystack_subscription_code = match["subscription_code"]
    subscription.paystack_email_token = match["email_token"]
    if subscription.cancel_at_period_end:
        await client.disable_subscription(subscription.paystack_subscription_code, subscription.paystack_email_token)
        checkout.renewal_state = "cancelled"
    else:
        checkout.renewal_state = "complete"
    checkout.authorization_code = None
    await db.commit()
    await publish_realtime_event(user_id, "billing.updated")
