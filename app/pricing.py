from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import ReferralCheckout, Subscription, User

PLAN_AMOUNTS = {"basic": 1_000_000, "pro": 2_000_000}
REFERRAL_DISCOUNT_PERCENT = 20


async def referral_offer(db: AsyncSession, user: User) -> dict | None:
    if not user.email_verified or not user.referred_by_user_id or user.first_paid_reference:
        return None
    checkout = await db.get(ReferralCheckout, user.id)
    if checkout and checkout.paid_at:
        return None
    # Honor a checkout already issued if referrals are subsequently switched off.
    if not get_settings().affiliate_enabled and not checkout:
        return None
    earlier = await db.scalar(select(Subscription.id).where(
        Subscription.user_id == user.id,
        Subscription.reference.is_not(None),
        Subscription.status.in_(["active", "grace", "cancelled", "replaced"]),
    ).limit(1))
    if earlier:
        return None
    return {
        "percent": REFERRAL_DISCOUNT_PERCENT,
        "first_payment_amounts": {plan: amount * 80 // 100 for plan, amount in PLAN_AMOUNTS.items()},
        "checkout_plan": checkout.plan if checkout else None,
        "checkout_url": checkout.authorization_url if checkout else None,
    }
