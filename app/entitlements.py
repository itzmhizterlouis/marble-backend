from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import ComplimentaryGrant, Subscription, TrialUsage, User, utcnow


@dataclass(frozen=True)
class BillingAccess:
    tier: str
    plan: str | None
    capabilities: dict[str, bool]
    access_until: datetime | None = None
    grace_until: datetime | None = None
    trial_consumed: bool = False
    trial_post_id: str | None = None
    cancel_at_period_end: bool = False
    enforcement_enabled: bool = False

    def payload(self) -> dict:
        return asdict(self)


def _aware(value: datetime | None) -> datetime | None:
    if value and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _capabilities(plan: str | None, *, trial: bool = False) -> dict[str, bool]:
    return {
        "publish": bool(plan in {"basic", "pro"} or trial),
        "schedule": bool(plan in {"basic", "pro"}),
        "analytics": plan == "pro",
        "ai_creation": plan == "pro",
        "history": True,
        "accounts": True,
        "billing": True,
    }


async def resolve_billing_access(db: AsyncSession, user: User) -> BillingAccess:
    settings = get_settings()
    if not settings.billing_enforcement_enabled:
        return BillingAccess(
            tier="pro",
            plan="pro",
            capabilities=_capabilities("pro"),
            enforcement_enabled=False,
        )

    now = datetime.now(UTC)
    trial = await db.scalar(select(TrialUsage).where(TrialUsage.user_id == user.id))
    grants = list(
        await db.scalars(
            select(ComplimentaryGrant)
            .where(
                ComplimentaryGrant.user_id == user.id,
                ComplimentaryGrant.starts_at <= now,
                ComplimentaryGrant.ends_at > now,
            )
            .order_by(ComplimentaryGrant.ends_at.desc())
        )
    )
    grant = next((item for item in grants if item.plan == "pro"), grants[0] if grants else None)

    subscription = await db.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == user.id,
            Subscription.status.in_(["active", "grace", "cancelled"]),
            or_(
                Subscription.paid_through.is_(None),
                Subscription.paid_through > now,
                Subscription.grace_until > now,
            ),
        )
        .order_by(Subscription.created_at.desc())
    )
    plan_rank = {None: 0, "basic": 1, "pro": 2}
    # A creator can upgrade from the launch Basic grant to paid Pro
    # immediately. Otherwise, preserve the highest active access so an
    # overlapping complimentary grant never removes capabilities.
    if subscription and plan_rank.get(subscription.plan, 0) >= plan_rank.get(grant.plan if grant else None, 0):
        paid_through = _aware(subscription.paid_through)
        grace_until = _aware(subscription.grace_until)
        in_grace = subscription.status == "grace" and bool(grace_until and grace_until > now)
        if subscription.status != "grace" or in_grace:
            return BillingAccess(
                tier="grace" if in_grace else subscription.plan,
                plan=subscription.plan,
                capabilities=_capabilities(subscription.plan),
                access_until=paid_through,
                grace_until=grace_until,
                trial_consumed=bool(trial),
                trial_post_id=trial.post_id if trial else None,
                cancel_at_period_end=subscription.cancel_at_period_end,
                enforcement_enabled=True,
            )

    if grant:
        return BillingAccess(
            tier="complimentary",
            plan=grant.plan,
            capabilities=_capabilities(grant.plan),
            access_until=_aware(grant.ends_at),
            trial_consumed=bool(trial),
            trial_post_id=trial.post_id if trial else None,
            enforcement_enabled=True,
        )

    if user.email_verified and not trial:
        return BillingAccess(
            tier="trial",
            plan=None,
            capabilities=_capabilities(None, trial=True),
            trial_consumed=False,
            enforcement_enabled=True,
        )
    return BillingAccess(
        tier="expired",
        plan=None,
        capabilities=_capabilities(None),
        trial_consumed=bool(trial),
        trial_post_id=trial.post_id if trial else None,
        enforcement_enabled=True,
    )


def upgrade_required(capability: str, *, message: str | None = None) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "code": "upgrade_required",
            "message": message or f"Upgrade your plan to use {capability.replace('_', ' ')}",
        },
    )


async def require_capability(db: AsyncSession, user: User, capability: str) -> BillingAccess:
    access = await resolve_billing_access(db, user)
    if not access.capabilities.get(capability, False):
        raise upgrade_required(capability)
    return access


async def reserve_trial_publication(
    db: AsyncSession,
    user: User,
    *,
    post_id: str,
    provider_account_ids: list[str],
) -> TrialUsage:
    # Locking the user row serializes first-publication attempts across different posts.
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    usage = await db.scalar(select(TrialUsage).where(TrialUsage.user_id == user.id))
    if usage:
        if usage.post_id == post_id:
            return usage
        raise upgrade_required("publish", message="Your one-post preview has been used. Choose a plan to continue")
    usage = TrialUsage(
        user_id=user.id,
        post_id=post_id,
        provider_account_ids=sorted(set(provider_account_ids)),
        status="consumed",
        reserved_at=utcnow(),
        consumed_at=utcnow(),
    )
    db.add(usage)
    await db.flush()
    return usage
