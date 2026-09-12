from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from celery.exceptions import CeleryError
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db
from .deps import get_current_user, require_verified_user
from .email import send_payment_attention_email
from .entitlements import resolve_billing_access
from .events import publish_realtime_event
from .models import BillingEvent, Subscription, User, utcnow
from .paystack import PaystackClient, PaystackError

router = APIRouter(prefix="/v1", tags=["billing"])
PLAN_AMOUNTS = {"basic": 1_000_000, "pro": 2_000_000}


class CheckoutIn(BaseModel):
    plan: str = Field(pattern="^(basic|pro)$")


class VerifyIn(BaseModel):
    reference: str = Field(min_length=3, max_length=255)


def _plan_code(plan: str) -> str:
    settings = get_settings()
    value = settings.paystack_basic_plan_code if plan == "basic" else settings.paystack_pro_plan_code
    if not value:
        raise HTTPException(
            status_code=503,
            detail={"code": "billing_not_configured", "message": f"The {plan.title()} plan is not configured"},
        )
    return value


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


async def activate_transaction(db: AsyncSession, user: User, data: dict, requested_plan: str | None = None) -> Subscription:
    reference = str(data.get("reference") or "")
    if data.get("status") != "success" or not reference:
        raise HTTPException(
            status_code=409,
            detail={"code": "payment_not_completed", "message": "This payment has not completed"},
        )
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    plan = requested_plan or metadata.get("reverb_plan")
    plan_payload = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    plan_code = str(plan_payload.get("plan_code") or data.get("plan_object", {}).get("plan_code") or "")
    if plan not in PLAN_AMOUNTS:
        settings = get_settings()
        plan = "basic" if plan_code == settings.paystack_basic_plan_code else "pro" if plan_code == settings.paystack_pro_plan_code else None
    if plan not in PLAN_AMOUNTS:
        raise HTTPException(status_code=400, detail={"code": "unknown_plan", "message": "Payment plan could not be verified"})
    if int(data.get("amount") or 0) != PLAN_AMOUNTS[plan] or str(data.get("currency") or "NGN").upper() != "NGN":
        raise HTTPException(
            status_code=400,
            detail={"code": "payment_amount_mismatch", "message": "Payment amount does not match the selected plan"},
        )
    metadata_user = metadata.get("reverb_user_id")
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    if metadata_user and str(metadata_user) != user.id:
        raise HTTPException(status_code=403, detail={"code": "payment_owner_mismatch", "message": "This payment belongs to another account"})
    if customer.get("email") and str(customer["email"]).casefold() != user.email.casefold():
        raise HTTPException(status_code=403, detail={"code": "payment_owner_mismatch", "message": "This payment belongs to another account"})
    existing = await db.scalar(select(Subscription).where(Subscription.reference == reference))
    if existing:
        return existing
    now = utcnow()
    subscription_data = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
    paid_through = _parse_time(subscription_data.get("next_payment_date")) or now + timedelta(days=31)
    subscription = Subscription(
        user_id=user.id,
        plan=plan,
        status="active",
        reference=reference,
        paystack_customer_code=str(customer.get("customer_code") or "") or None,
        paystack_subscription_code=str(subscription_data.get("subscription_code") or "") or None,
        paystack_email_token=str(subscription_data.get("email_token") or "") or None,
        amount_kobo=PLAN_AMOUNTS[plan],
        currency="NGN",
        paid_through=paid_through,
    )
    previous = await db.scalar(
        select(Subscription)
        .where(Subscription.user_id == user.id, Subscription.status.in_(["active", "grace"]))
        .order_by(Subscription.created_at.desc())
    )
    db.add(subscription)
    if previous:
        previous.status = "replaced"
        previous.cancel_at_period_end = True
    await db.commit()
    await publish_realtime_event(user.id, "billing.updated")
    if plan == "pro":
        try:
            from .tasks import sync_analytics

            sync_analytics.delay(user.id, True)
        except CeleryError:
            pass
    if previous and previous.paystack_subscription_code and previous.paystack_email_token:
        try:
            await PaystackClient().disable_subscription(previous.paystack_subscription_code, previous.paystack_email_token)
        except PaystackError:
            pass
    return subscription


@router.get("/billing/plans")
async def plans():
    return {
        "currency": "NGN",
        "plans": [
            {"id": "basic", "name": "Basic", "amount_kobo": PLAN_AMOUNTS["basic"], "interval": "monthly", "capabilities": ["publish", "schedule", "history", "accounts"]},
            {"id": "pro", "name": "Pro", "amount_kobo": PLAN_AMOUNTS["pro"], "interval": "monthly", "capabilities": ["publish", "schedule", "history", "accounts", "analytics", "ai_creation"]},
        ],
    }


@router.get("/billing/me")
async def billing_me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return (await resolve_billing_access(db, user)).payload()


@router.post("/billing/checkout")
async def checkout(payload: CheckoutIn, user: User = Depends(require_verified_user)):
    try:
        data = await PaystackClient().initialize_checkout(
            email=user.email, plan_code=_plan_code(payload.plan), plan=payload.plan, user_id=user.id
        )
    except PaystackError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": "checkout_failed", "message": str(exc)}) from exc
    return {"authorization_url": data.get("authorization_url"), "reference": data.get("reference")}


@router.post("/billing/verify")
async def verify_payment(payload: VerifyIn, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    try:
        data = await PaystackClient().verify_transaction(payload.reference)
    except PaystackError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": "payment_verification_failed", "message": str(exc)}) from exc
    await activate_transaction(db, user, data)
    return (await resolve_billing_access(db, user)).payload()


@router.post("/billing/cancel")
async def cancel_subscription(user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    subscription = await db.scalar(
        select(Subscription).where(Subscription.user_id == user.id, Subscription.status.in_(["active", "grace"])).order_by(Subscription.created_at.desc())
    )
    if not subscription:
        raise HTTPException(status_code=404, detail={"code": "subscription_not_found", "message": "No active subscription found"})
    if subscription.paystack_subscription_code and subscription.paystack_email_token:
        try:
            await PaystackClient().disable_subscription(subscription.paystack_subscription_code, subscription.paystack_email_token)
        except PaystackError as exc:
            raise HTTPException(status_code=exc.status_code, detail={"code": "cancellation_failed", "message": str(exc)}) from exc
    subscription.cancel_at_period_end = True
    subscription.cancelled_at = utcnow()
    await db.commit()
    await publish_realtime_event(user.id, "billing.updated")
    return (await resolve_billing_access(db, user)).payload()


@router.post("/billing/manage")
async def manage_payment(user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    subscription = await db.scalar(
        select(Subscription).where(Subscription.user_id == user.id, Subscription.paystack_subscription_code.is_not(None)).order_by(Subscription.created_at.desc())
    )
    if not subscription:
        raise HTTPException(status_code=404, detail={"code": "subscription_not_found", "message": "No subscription found"})
    try:
        data = await PaystackClient().manage_link(subscription.paystack_subscription_code)
    except PaystackError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": "billing_management_failed", "message": str(exc)}) from exc
    return {"url": data.get("link") or data.get("url")}


@router.post("/webhooks/paystack")
async def paystack_webhook(
    request: Request,
    signature: str | None = Header(default=None, alias="x-paystack-signature"),
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()
    secret = get_settings().paystack_secret_key
    expected = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest() if secret else ""
    if not signature or not expected or not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail={"code": "invalid_webhook_signature", "message": "Invalid webhook signature"})
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_webhook_payload", "message": "Webhook body must be JSON"}) from exc
    event_id = hashlib.sha256(body).hexdigest()
    if not await db.scalar(select(BillingEvent.id).where(BillingEvent.provider_event_id == event_id)):
        event = BillingEvent(id=str(__import__("uuid").uuid4()), provider_event_id=event_id, event_type=str(payload.get("event") or "unknown"), payload=payload)
        db.add(event)
        await db.commit()
        try:
            from .tasks import process_billing_event
            process_billing_event.delay(event.id)
        except CeleryError:
            pass
    return {"message": "Accepted"}


async def apply_billing_event(db: AsyncSession, event: BillingEvent) -> None:
    if event.processed_at:
        return
    payload = event.payload or {}
    name = str(payload.get("event") or event.event_type)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    email = str(customer.get("email") or data.get("email") or "").casefold()
    user = await db.scalar(select(User).where(User.email == email)) if email else None
    reference = str(data.get("reference") or "")
    code = str(data.get("subscription_code") or "")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    plan_data = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    nested_subscription = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
    nested_plan = nested_subscription.get("plan") if isinstance(nested_subscription.get("plan"), dict) else {}
    plan_code = str(plan_data.get("plan_code") or nested_plan.get("plan_code") or "")
    settings = get_settings()
    event_plan = metadata.get("reverb_plan")
    if event_plan not in PLAN_AMOUNTS:
        event_plan = (
            "basic"
            if plan_code == settings.paystack_basic_plan_code
            else "pro"
            if plan_code == settings.paystack_pro_plan_code
            else None
        )
    payment_matches_plan = name != "charge.success" or bool(
        event_plan in PLAN_AMOUNTS
        and int(data.get("amount") or 0) == PLAN_AMOUNTS[event_plan]
        and str(data.get("currency") or "NGN").upper() == "NGN"
    )
    subscription = None
    if reference:
        subscription = await db.scalar(select(Subscription).where(Subscription.reference == reference))
    if code:
        subscription = subscription or await db.scalar(
            select(Subscription).where(Subscription.paystack_subscription_code == code)
        )
    # Recurring events may omit a transaction reference. A new successful
    # checkout must not fall back to the old subscription during a plan change.
    if not subscription and user and name not in {"charge.success", "subscription.create"}:
        subscription = await db.scalar(select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.created_at.desc()))
    if not subscription and user and name == "subscription.create" and event_plan in PLAN_AMOUNTS:
        subscription = await db.scalar(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.plan == event_plan,
                Subscription.paystack_subscription_code.is_(None),
            )
            .order_by(Subscription.created_at.desc())
        )
    previous_to_disable: Subscription | None = None
    if not subscription and user and payment_matches_plan and name in {
        "subscription.create",
        "charge.success",
        "invoice.update",
        "invoice.payment_failed",
        "charge.failed",
    }:
        plan = event_plan
        if plan in PLAN_AMOUNTS:
            previous_to_disable = await db.scalar(
                select(Subscription)
                .where(Subscription.user_id == user.id, Subscription.status.in_(["active", "grace"]))
                .order_by(Subscription.created_at.desc())
            )
            subscription = Subscription(
                user_id=user.id,
                plan=plan,
                status="pending",
                reference=reference or None,
                paystack_customer_code=str(customer.get("customer_code") or "") or None,
                paystack_subscription_code=code or str(nested_subscription.get("subscription_code") or "") or None,
                paystack_email_token=str(data.get("email_token") or nested_subscription.get("email_token") or "") or None,
                amount_kobo=PLAN_AMOUNTS[plan],
                currency="NGN",
            )
            db.add(subscription)
    if subscription:
        subscription.paystack_subscription_code = code or subscription.paystack_subscription_code
        subscription.paystack_email_token = str(data.get("email_token") or "") or subscription.paystack_email_token
        subscription.paystack_customer_code = str(customer.get("customer_code") or "") or subscription.paystack_customer_code
        next_payment = _parse_time(data.get("next_payment_date"))
        if (
            payment_matches_plan
            and name in {"subscription.create", "charge.success", "invoice.update"}
            and data.get("status") in {None, "success", "active"}
        ):
            subscription.status = "active"
            subscription.grace_until = None
            subscription.paid_through = next_payment or subscription.paid_through or utcnow() + timedelta(days=31)
            if previous_to_disable:
                previous_to_disable.status = "replaced"
                previous_to_disable.cancel_at_period_end = True
        elif name in {"invoice.payment_failed", "charge.failed"}:
            subscription.status = "grace"
            subscription.grace_until = utcnow() + timedelta(days=3)
            if user:
                try:
                    await send_payment_attention_email(
                        user.email, user.name, subscription.grace_until.isoformat()
                    )
                except Exception:
                    pass
        elif name in {"subscription.disable", "subscription.not_renew"}:
            subscription.cancel_at_period_end = True
            subscription.cancelled_at = utcnow()
    event.processed_at = utcnow()
    await db.commit()
    if subscription:
        await publish_realtime_event(subscription.user_id, "billing.updated")
        if subscription.status == "active" and subscription.plan == "pro":
            try:
                from .tasks import sync_analytics

                sync_analytics.delay(subscription.user_id, True)
            except CeleryError:
                pass
    if previous_to_disable and previous_to_disable.paystack_subscription_code and previous_to_disable.paystack_email_token:
        try:
            await PaystackClient().disable_subscription(
                previous_to_disable.paystack_subscription_code,
                previous_to_disable.paystack_email_token,
            )
        except PaystackError:
            pass
