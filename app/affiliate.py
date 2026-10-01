from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .admin import require_admin
from .config import get_settings
from .database import get_db
from .deps import require_verified_user
from .events import publish_realtime_event
from .models import (
    AffiliateAuditEvent,
    AffiliateBankRecipient,
    AffiliateCommission,
    AffiliateLedgerEntry,
    AffiliatePaymentReversal,
    AffiliatePayout,
    Subscription,
    User,
    utcnow,
)
from .paystack import PaystackClient, PaystackError

router = APIRouter(prefix="/v1", tags=["referrals"])
MIN_WITHDRAWAL_KOBO = 200_000
MAX_TRANSFER_KOBO = 1_000_000_000


def require_affiliate_enabled() -> None:
    if not get_settings().affiliate_enabled:
        raise HTTPException(status_code=503, detail={"code": "referrals_unavailable", "message": "Referrals are not available yet"})


def new_referral_code() -> str:
    # 64 random bits, uppercase and readable in a signup URL.
    return secrets.token_hex(8).upper()


async def resolve_referrer(db: AsyncSession, code: str | None) -> User | None:
    if not code:
        return None
    require_affiliate_enabled()
    referrer = await db.scalar(select(User).where(User.referral_code == code.strip().upper(), User.email_verified.is_(True)))
    if not referrer:
        raise HTTPException(status_code=400, detail={"code": "invalid_referral_code", "message": "This referral code is not valid"})
    return referrer


async def record_first_payment(db: AsyncSession, user: User, reference: str, amount_kobo: int, subscription_id: str | None = None, paid_at: datetime | None = None, *, commission_base_kobo: int | None = None) -> AffiliateCommission | None:
    if not reference or amount_kobo <= 0:
        return None
    locked = await db.scalar(select(User).where(User.id == user.id).with_for_update())
    if not locked or locked.first_paid_reference:
        return None
    earlier_payment = await db.scalar(
        select(Subscription.reference)
        .where(
            Subscription.user_id == user.id,
            Subscription.reference.is_not(None),
            Subscription.reference != reference,
            Subscription.status.in_(["active", "grace", "cancelled", "replaced"]),
        )
        .order_by(Subscription.created_at.asc())
        .limit(1)
    )
    if earlier_payment:
        locked.first_paid_reference = earlier_payment
        return None
    locked.first_paid_reference = reference
    if not locked.referred_by_user_id:
        return None
    if await db.get(AffiliatePaymentReversal, reference):
        return None
    base = commission_base_kobo if commission_base_kobo is not None else amount_kobo
    amount = base * 20 // 100
    payment_time = paid_at.replace(tzinfo=UTC) if paid_at and paid_at.tzinfo is None else paid_at or utcnow()
    commission = AffiliateCommission(
        referrer_user_id=locked.referred_by_user_id,
        referred_user_id=locked.id,
        payment_reference=reference,
        subscription_id=subscription_id,
        payment_amount_kobo=amount_kobo,
        commission_base_kobo=base,
        amount_kobo=amount,
        available_at=payment_time + timedelta(days=7),
    )
    db.add(commission)
    await db.flush()
    db.add(AffiliateLedgerEntry(user_id=commission.referrer_user_id, commission_id=commission.id, kind="commission", amount_kobo=amount, available_at=commission.available_at))
    return commission


async def reverse_commission(db: AsyncSession, payment_reference: str, reason: str) -> str | None:
    await db.execute(text("INSERT INTO affiliate_payment_reversals (payment_reference, reason, created_at) VALUES (:reference, :reason, :created_at) ON CONFLICT (payment_reference) DO NOTHING"), {"reference": payment_reference, "reason": reason, "created_at": utcnow()})
    commission = await db.scalar(select(AffiliateCommission).where(AffiliateCommission.payment_reference == payment_reference).with_for_update())
    if not commission or commission.reversed_at:
        return None
    now = utcnow()
    commission.reversed_at = now
    commission.reversal_reason = reason
    commission.requires_review = True
    release = commission.available_at if commission.available_at.replace(tzinfo=UTC) > now else now
    db.add(AffiliateLedgerEntry(user_id=commission.referrer_user_id, commission_id=commission.id, kind="reversal", amount_kobo=-commission.amount_kobo, available_at=release))
    db.add(AffiliateAuditEvent(user_id=commission.referrer_user_id, action="commission_reversed", details={"reference": payment_reference, "reason": reason}))
    return commission.referrer_user_id


async def available_balance(db: AsyncSession, user_id: str) -> int:
    return int(await db.scalar(select(func.coalesce(func.sum(AffiliateLedgerEntry.amount_kobo), 0)).where(AffiliateLedgerEntry.user_id == user_id, AffiliateLedgerEntry.available_at <= utcnow())) or 0)


def payout_quote(requested_kobo: int) -> dict:
    if requested_kobo < MIN_WITHDRAWAL_KOBO:
        raise HTTPException(status_code=422, detail={"code": "withdrawal_too_small", "message": "Withdraw at least ₦2,000"})
    # Paystack's September 2026 NGN schedule. The backend owns the quote; a
    # changed schedule requires new creator consent before owner approval.
    # At fee-band boundaries, requested = net + fee(net) may have no exact
    # solution. Leave any tiny remainder in the creator's available balance.
    net = requested_kobo
    for candidate in range(requested_kobo, max(0, requested_kobo - 10_001), -1):
        transfer_fee = 1000 if candidate <= 500_000 else 2500 if candidate <= 5_000_000 else 5000
        stamp_duty = 5000 if candidate >= 1_000_000 else 0
        candidate_fee = transfer_fee + stamp_duty
        if candidate + candidate_fee <= requested_kobo:
            net = candidate
            fee = candidate_fee
            break
    else:
        raise HTTPException(status_code=422, detail={"code": "invalid_payout", "message": "Could not quote this transfer"})
    if net < 5_000:
        raise HTTPException(status_code=422, detail={"code": "withdrawal_too_small", "message": "Net transfer must be at least ₦50"})
    if net > MAX_TRANSFER_KOBO:
        raise HTTPException(status_code=422, detail={"code": "withdrawal_too_large", "message": "A single bank transfer cannot exceed ₦10,000,000"})
    return {"requested_kobo": net + fee, "fee_kobo": fee, "net_kobo": net, "currency": "NGN"}


def recipient_out(recipient: AffiliateBankRecipient | None) -> dict | None:
    if not recipient:
        return None
    return {"bank_name": recipient.bank_name, "bank_code": recipient.bank_code, "account_name": recipient.account_name, "account_last_four": recipient.account_last_four}


def payout_out(payout: AffiliatePayout) -> dict:
    return {"id": payout.id, "status": payout.status, "requested_kobo": payout.requested_kobo, "fee_kobo": payout.fee_kobo, "net_kobo": payout.net_kobo, "bank_name": payout.bank_name, "account_name": payout.account_name, "account_last_four": payout.account_last_four, "created_at": payout.created_at, "completed_at": payout.completed_at}


class BankIn(BaseModel):
    bank_code: str = Field(min_length=2, max_length=32)
    account_number: str = Field(pattern=r"^[0-9]{10}$")
    account_name: str = Field(min_length=2, max_length=255)


class BankLookupIn(BaseModel):
    bank_code: str = Field(min_length=2, max_length=32)
    account_number: str = Field(pattern=r"^[0-9]{10}$")


class AmountIn(BaseModel):
    amount_kobo: int = Field(ge=MIN_WITHDRAWAL_KOBO)


class PayoutIn(AmountIn):
    recipient_id: str
    quoted_fee_kobo: int = Field(ge=0)
    quoted_net_kobo: int = Field(gt=0)


class ReconfirmIn(BaseModel):
    quoted_requested_kobo: int = Field(gt=0)
    quoted_fee_kobo: int = Field(ge=0)
    quoted_net_kobo: int = Field(gt=0)


class DecisionIn(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


class OTPIn(BaseModel):
    otp: str = Field(min_length=4, max_length=12)


@router.get("/referrals/validate")
async def validate_code(code: str = Query(min_length=6, max_length=20), db: AsyncSession = Depends(get_db)):
    require_affiliate_enabled()
    return {"valid": bool(await db.scalar(select(User.id).where(User.referral_code == code.strip().upper(), User.email_verified.is_(True))))}


@router.get("/referrals/status")
async def referral_status():
    return {"enabled": get_settings().affiliate_enabled}


@router.get("/referrals/me")
async def my_referrals(user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    require_affiliate_enabled()
    if not user.referral_code:
        user.referral_code = new_referral_code()
        await db.commit()
    commissions = (await db.scalars(select(AffiliateCommission).where(AffiliateCommission.referrer_user_id == user.id).order_by(AffiliateCommission.created_at.desc()))).all()
    payouts = (await db.scalars(select(AffiliatePayout).where(AffiliatePayout.user_id == user.id).order_by(AffiliatePayout.created_at.desc()).limit(50))).all()
    recipient = await db.scalar(select(AffiliateBankRecipient).where(AffiliateBankRecipient.user_id == user.id))
    now = utcnow()
    return {
        "code": user.referral_code,
        "signup_url": f"{get_settings().frontend_url}/signup?ref={user.referral_code}",
        "referral_count": int(await db.scalar(select(func.count(User.id)).where(User.referred_by_user_id == user.id)) or 0),
        "pending_kobo": sum(item.amount_kobo for item in commissions if not item.reversed_at and item.available_at.replace(tzinfo=UTC) > now),
        "available_kobo": await available_balance(db, user.id),
        "recipient": recipient_out(recipient),
        "commissions": [{"id": item.id, "amount_kobo": item.amount_kobo, "payment_amount_kobo": item.payment_amount_kobo, "available_at": item.available_at, "status": "reversed" if item.reversed_at else "pending" if item.available_at.replace(tzinfo=UTC) > now else "available"} for item in commissions[:50]],
        "payouts": [payout_out(item) for item in payouts],
    }


@router.get("/referrals/banks")
async def list_banks(user: User = Depends(require_verified_user)):
    del user
    require_affiliate_enabled()
    try:
        banks = await PaystackClient().banks()
    except PaystackError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": "banks_unavailable", "message": str(exc)}) from exc
    return {"items": [{"name": item.get("name"), "code": item.get("code")} for item in banks if isinstance(item, dict) and item.get("active", True)]}


@router.post("/referrals/bank")
async def save_bank(payload: BankIn, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    require_affiliate_enabled()
    client = PaystackClient()
    try:
        banks = await client.banks()
        bank = next((item for item in banks if str(item.get("code")) == payload.bank_code), None)
        if not bank:
            raise HTTPException(status_code=422, detail={"code": "invalid_bank", "message": "Choose a listed Nigerian bank"})
        resolved = await client.resolve_account(payload.bank_code, payload.account_number)
        name = str(resolved.get("account_name") or "").strip()
        if not name or name.casefold() != payload.account_name.strip().casefold():
            raise HTTPException(status_code=409, detail={"code": "account_name_changed", "message": "Confirm the account name shown by your bank"})
        data = await client.create_transfer_recipient(name, payload.bank_code, payload.account_number)
    except PaystackError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": "bank_verification_failed", "message": str(exc)}) from exc
    code = str(data.get("recipient_code") or "")
    if not code:
        raise HTTPException(status_code=502, detail={"code": "bank_setup_failed", "message": "Paystack did not confirm this bank recipient"})
    recipient = await db.scalar(select(AffiliateBankRecipient).where(AffiliateBankRecipient.user_id == user.id))
    if not recipient:
        recipient = AffiliateBankRecipient(user_id=user.id, recipient_code=code, bank_code=payload.bank_code, bank_name=str(bank.get("name")), account_name=name, account_last_four=payload.account_number[-4:])
        db.add(recipient)
    else:
        recipient.recipient_code = code
        recipient.bank_code = payload.bank_code
        recipient.bank_name = str(bank.get("name"))
        recipient.account_name = name
        recipient.account_last_four = payload.account_number[-4:]
    db.add(AffiliateAuditEvent(user_id=user.id, actor_user_id=user.id, action="bank_updated", details={"bank_code": payload.bank_code, "last_four": payload.account_number[-4:]}))
    await db.commit()
    return recipient_out(recipient)


@router.post("/referrals/bank/resolve")
async def lookup_bank(payload: BankLookupIn, user: User = Depends(require_verified_user)):
    del user
    require_affiliate_enabled()
    try:
        data = await PaystackClient().resolve_account(payload.bank_code, payload.account_number)
    except PaystackError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": "bank_verification_failed", "message": str(exc)}) from exc
    return {"account_name": data.get("account_name"), "account_last_four": payload.account_number[-4:]}


@router.post("/referrals/payouts/quote")
async def quote_payout(payload: AmountIn, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    require_affiliate_enabled()
    if payload.amount_kobo > await available_balance(db, user.id):
        raise HTTPException(status_code=409, detail={"code": "insufficient_commission", "message": "That amount is not available to withdraw"})
    recipient = await db.scalar(select(AffiliateBankRecipient).where(AffiliateBankRecipient.user_id == user.id))
    if not recipient:
        raise HTTPException(status_code=409, detail={"code": "bank_required", "message": "Set up a bank account first"})
    return {**payout_quote(payload.amount_kobo), "recipient_id": recipient.id}


@router.post("/referrals/payouts", status_code=201)
async def request_payout(payload: PayoutIn, idempotency_key: str = Header(min_length=8, max_length=255, alias="Idempotency-Key"), user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    require_affiliate_enabled()
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    existing = await db.scalar(select(AffiliatePayout).where(AffiliatePayout.user_id == user.id, AffiliatePayout.idempotency_key == idempotency_key))
    if existing:
        return payout_out(existing)
    quote = payout_quote(payload.amount_kobo)
    if quote["requested_kobo"] != payload.amount_kobo or quote["fee_kobo"] != payload.quoted_fee_kobo or quote["net_kobo"] != payload.quoted_net_kobo:
        raise HTTPException(status_code=409, detail={"code": "payout_quote_changed", "message": "Review the updated transfer fee before requesting a payout"})
    if payload.amount_kobo > await available_balance(db, user.id):
        raise HTTPException(status_code=409, detail={"code": "insufficient_commission", "message": "That amount is not available to withdraw"})
    recipient = await db.scalar(select(AffiliateBankRecipient).where(AffiliateBankRecipient.user_id == user.id))
    if not recipient:
        raise HTTPException(status_code=409, detail={"code": "bank_required", "message": "Set up a bank account first"})
    if payload.recipient_id != recipient.id:
        raise HTTPException(status_code=409, detail={"code": "bank_changed", "message": "Review the withdrawal for your current bank account"})
    payout = AffiliatePayout(user_id=user.id, recipient_code=recipient.recipient_code, bank_name=recipient.bank_name, account_name=recipient.account_name, account_last_four=recipient.account_last_four, requested_kobo=payload.amount_kobo, fee_kobo=quote["fee_kobo"], net_kobo=quote["net_kobo"], idempotency_key=idempotency_key, transfer_reference=f"rvb_{uuid.uuid4().hex}")
    db.add(payout)
    await db.flush()
    db.add(AffiliateLedgerEntry(user_id=user.id, payout_id=payout.id, kind="payout_reserved", amount_kobo=-payload.amount_kobo, available_at=utcnow()))
    db.add(AffiliateAuditEvent(user_id=user.id, actor_user_id=user.id, payout_id=payout.id, action="payout_requested", details={"requested_kobo": payload.amount_kobo, "fee_kobo": quote["fee_kobo"]}))
    await db.commit()
    await publish_realtime_event(user.id, "referrals.updated")
    return payout_out(payout)


@router.post("/referrals/payouts/{payout_id}/reconfirm")
async def reconfirm_payout(payout_id: str, payload: ReconfirmIn, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    require_affiliate_enabled()
    payout = await db.scalar(select(AffiliatePayout).where(AffiliatePayout.id == payout_id, AffiliatePayout.user_id == user.id).with_for_update())
    if not payout or payout.status != "needs_reconfirmation":
        raise HTTPException(status_code=409, detail={"code": "payout_not_reconfirmable", "message": "This payout does not need a new confirmation"})
    quote = payout_quote(payout.requested_kobo)
    if quote["requested_kobo"] != payload.quoted_requested_kobo or quote["fee_kobo"] != payload.quoted_fee_kobo or quote["net_kobo"] != payload.quoted_net_kobo:
        raise HTTPException(status_code=409, detail={"code": "payout_quote_changed", "message": "Review the updated transfer fee"})
    if quote["requested_kobo"] < payout.requested_kobo:
        db.add(AffiliateLedgerEntry(user_id=user.id, payout_id=payout.id, kind="payout_released", amount_kobo=payout.requested_kobo - quote["requested_kobo"], available_at=utcnow()))
    payout.requested_kobo = quote["requested_kobo"]
    payout.fee_kobo = quote["fee_kobo"]
    payout.net_kobo = quote["net_kobo"]
    payout.status = "requested"
    db.add(AffiliateAuditEvent(user_id=user.id, actor_user_id=user.id, payout_id=payout.id, action="payout_fee_reconfirmed", details={"fee_kobo": quote["fee_kobo"]}))
    await db.commit()
    await publish_realtime_event(user.id, "referrals.updated")
    return payout_out(payout)


@router.get("/referrals/payouts/{payout_id}/quote")
async def requote_payout(payout_id: str, user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    require_affiliate_enabled()
    payout = await db.scalar(select(AffiliatePayout).where(AffiliatePayout.id == payout_id, AffiliatePayout.user_id == user.id))
    if not payout or payout.status != "needs_reconfirmation":
        raise HTTPException(status_code=409, detail={"code": "payout_not_reconfirmable", "message": "This payout does not need a new confirmation"})
    return payout_quote(payout.requested_kobo)


async def _release_payout(db: AsyncSession, payout: AffiliatePayout, status: str, action: str) -> bool:
    if payout.status in {"rejected", "failed", "reversed"}:
        return False
    if payout.status == "paid" and status == "failed":
        return False
    payout.status = status
    db.add(AffiliateLedgerEntry(user_id=payout.user_id, payout_id=payout.id, kind="payout_released", amount_kobo=payout.requested_kobo, available_at=utcnow()))
    db.add(AffiliateAuditEvent(user_id=payout.user_id, payout_id=payout.id, action=action))
    return True


def _apply_transfer_status(payout: AffiliatePayout, status: str) -> bool:
    if status == "success" and payout.status not in {"paid", "rejected", "failed", "reversed"}:
        payout.status = "paid"
        payout.completed_at = utcnow()
        return True
    return False


async def apply_transfer_event(db: AsyncSession, data: dict, event_name: str) -> str | None:
    reference = str(data.get("reference") or "")
    payout = await db.scalar(select(AffiliatePayout).where(AffiliatePayout.transfer_reference == reference).with_for_update()) if reference else None
    if not payout:
        return None
    if event_name == "transfer.success":
        if payout.status in {"failed", "reversed"}:
            verified = await PaystackClient().verify_transfer(reference)
            if str(verified.get("status") or "").lower() == "success":
                payout.status = "paid"
                payout.completed_at = utcnow()
                db.add(AffiliateLedgerEntry(user_id=payout.user_id, payout_id=payout.id, kind="payout_reserved", amount_kobo=-payout.requested_kobo, available_at=utcnow()))
                db.add(AffiliateAuditEvent(user_id=payout.user_id, payout_id=payout.id, action="late_transfer_success_verified"))
        _apply_transfer_status(payout, "success")
    elif event_name in {"transfer.failed", "transfer.reversed"}:
        await _release_payout(db, payout, "failed" if event_name.endswith("failed") else "reversed", event_name)
    return payout.user_id


@router.get("/admin/referrals/payouts")
async def admin_payouts(admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    del admin
    require_affiliate_enabled()
    rows = (await db.execute(select(AffiliatePayout, User.email).join(User, AffiliatePayout.user_id == User.id).order_by(AffiliatePayout.created_at.desc()).limit(100))).all()
    items = []
    for payout, email in rows:
        audit = (await db.scalars(select(AffiliateAuditEvent).where(AffiliateAuditEvent.payout_id == payout.id).order_by(AffiliateAuditEvent.created_at.desc()).limit(10))).all()
        items.append({
            **payout_out(payout),
            "creator_email": email,
            "available_kobo": await available_balance(db, payout.user_id),
            "reversed_commissions_for_review": int(await db.scalar(select(func.count(AffiliateCommission.id)).where(AffiliateCommission.referrer_user_id == payout.user_id, AffiliateCommission.requires_review.is_(True))) or 0),
            "audit_events": [{"action": item.action, "created_at": item.created_at, "details": item.details} for item in audit],
        })
    return {"items": items}


@router.post("/admin/referrals/payouts/{payout_id}/reject")
async def reject_payout(payout_id: str, payload: DecisionIn, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    require_affiliate_enabled()
    payout = await db.scalar(select(AffiliatePayout).where(AffiliatePayout.id == payout_id).with_for_update())
    if not payout:
        raise HTTPException(status_code=404, detail={"code": "payout_not_found", "message": "Payout not found"})
    if payout.status not in {"requested", "needs_reconfirmation"}:
        raise HTTPException(status_code=409, detail={"code": "payout_already_started", "message": "This payout can no longer be rejected"})
    await _release_payout(db, payout, "rejected", "payout_rejected")
    payout.approved_by_user_id = admin.id
    payout.decision_reason = payload.reason
    await db.commit()
    await publish_realtime_event(payout.user_id, "referrals.updated")
    return payout_out(payout)


async def _update_transfer_response(db: AsyncSession, payout: AffiliatePayout, data: dict, *, verified: bool = True) -> None:
    payout.transfer_code = str(data.get("transfer_code") or "") or payout.transfer_code
    status = str(data.get("status") or "").lower()
    if payout.status == "paid" and status not in {"reversed", "failed"}:
        return
    if not _apply_transfer_status(payout, status if verified else "pending"):
        if status in {"failed", "reversed"}:
            await _release_payout(db, payout, status, f"transfer_{status}")
        elif status == "otp":
            payout.status = "otp_required"
        else:
            payout.status = "pending"


@router.post("/admin/referrals/payouts/{payout_id}/approve")
async def approve_payout(payout_id: str, payload: DecisionIn, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    require_affiliate_enabled()
    payout = await db.scalar(select(AffiliatePayout).where(AffiliatePayout.id == payout_id).with_for_update())
    if not payout:
        raise HTTPException(status_code=404, detail={"code": "payout_not_found", "message": "Payout not found"})
    if payout.status not in {"requested", "uncertain"}:
        raise HTTPException(status_code=409, detail={"code": "payout_already_started", "message": "Check this payout’s current status"})
    if payout.status == "requested" and payout_quote(payout.requested_kobo)["fee_kobo"] != payout.fee_kobo:
        payout.status = "needs_reconfirmation"
        db.add(AffiliateAuditEvent(user_id=payout.user_id, actor_user_id=admin.id, payout_id=payout.id, action="payout_fee_changed"))
        await db.commit()
        await publish_realtime_event(payout.user_id, "referrals.updated")
        raise HTTPException(status_code=409, detail={"code": "payout_quote_changed", "message": "The creator must confirm the new transfer fee"})
    if await available_balance(db, payout.user_id) < 0:
        raise HTTPException(status_code=409, detail={"code": "commission_reversed", "message": "Earnings changed since this request; review before paying"})
    payout.status = "uncertain"
    payout.approved_by_user_id = admin.id
    payout.decision_reason = payload.reason
    db.add(AffiliateAuditEvent(user_id=payout.user_id, actor_user_id=admin.id, payout_id=payout.id, action="payout_approved", details={"reason": payload.reason}))
    await db.commit()
    client = PaystackClient()
    try:
        try:
            data = await client.verify_transfer(payout.transfer_reference)
            verified = True
        except PaystackError as exc:
            if exc.status_code != 404:
                raise
            data = await client.initiate_transfer(payout.transfer_reference, payout.recipient_code, payout.net_kobo)
            verified = False
    except PaystackError as exc:
        raise HTTPException(status_code=502, detail={"code": "transfer_unconfirmed", "message": f"Transfer status is uncertain; reconcile reference before retrying: {exc}"}) from exc
    await db.refresh(payout, with_for_update=True)
    await _update_transfer_response(db, payout, data, verified=verified)
    await db.commit()
    await publish_realtime_event(payout.user_id, "referrals.updated")
    return payout_out(payout)


@router.post("/admin/referrals/payouts/{payout_id}/otp")
async def finalize_payout(payout_id: str, payload: OTPIn, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    require_affiliate_enabled()
    payout = await db.scalar(select(AffiliatePayout).where(AffiliatePayout.id == payout_id).with_for_update())
    if not payout or payout.status != "otp_required" or not payout.transfer_code:
        raise HTTPException(status_code=409, detail={"code": "otp_not_required", "message": "This transfer does not need OTP approval"})
    try:
        data = await PaystackClient().finalize_transfer(payout.transfer_code, payload.otp)
    except PaystackError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": "otp_failed", "message": str(exc)}) from exc
    await db.refresh(payout, with_for_update=True)
    db.add(AffiliateAuditEvent(user_id=payout.user_id, actor_user_id=admin.id, payout_id=payout.id, action="transfer_otp_submitted"))
    await _update_transfer_response(db, payout, data, verified=False)
    await db.commit()
    await publish_realtime_event(payout.user_id, "referrals.updated")
    return payout_out(payout)


@router.post("/admin/referrals/payouts/{payout_id}/reconcile")
async def reconcile_payout(payout_id: str, admin: User = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    require_affiliate_enabled()
    payout = await db.scalar(select(AffiliatePayout).where(AffiliatePayout.id == payout_id).with_for_update())
    if not payout:
        raise HTTPException(status_code=404, detail={"code": "payout_not_found", "message": "Payout not found"})
    if payout.status in {"rejected", "failed", "reversed"}:
        return payout_out(payout)
    try:
        data = await PaystackClient().verify_transfer(payout.transfer_reference)
    except PaystackError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": "transfer_unconfirmed", "message": str(exc)}) from exc
    await db.refresh(payout, with_for_update=True)
    db.add(AffiliateAuditEvent(user_id=payout.user_id, actor_user_id=admin.id, payout_id=payout.id, action="transfer_reconciled"))
    await _update_transfer_response(db, payout, data)
    await db.commit()
    await publish_realtime_event(payout.user_id, "referrals.updated")
    return payout_out(payout)
