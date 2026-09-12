from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db
from .deps import get_current_user
from .entitlements import resolve_billing_access
from .events import publish_realtime_event
from .models import (
    ComplimentaryGrant,
    EntitlementAuditEvent,
    FeatureFlag,
    Subscription,
    TrialUsage,
    User,
    utcnow,
)
from .paystack import PaystackClient, PaystackError

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class GrantIn(BaseModel):
    plan: str = Field(pattern="^(basic|pro)$")
    reason: str = Field(min_length=5, max_length=1000)


class TrialResetIn(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


class AIFeatureIn(BaseModel):
    enabled: bool
    reason: str = Field(min_length=5, max_length=1000)


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.email.casefold() not in get_settings().admin_email_set:
        raise HTTPException(status_code=403, detail={"code": "admin_required", "message": "Owner access required"})
    return user


@router.get("/users")
async def list_users(
    q: str = Query(default="", max_length=160),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    del admin
    query = select(User).order_by(User.created_at.desc()).limit(100)
    if q.strip():
        search = f"%{q.strip()}%"
        query = query.where(or_(User.email.ilike(search), User.name.ilike(search)))
    users = list(await db.scalars(query))
    return {
        "items": [
            {
                "id": item.id,
                "name": item.name,
                "email": item.email,
                "email_verified": item.email_verified,
                "created_at": item.created_at,
                "billing": (await resolve_billing_access(db, item)).payload(),
            }
            for item in users
        ]
    }


@router.post("/users/{user_id}/complimentary-access")
async def grant_access(
    user_id: str,
    payload: GrantIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail={"code": "user_not_found", "message": "Creator not found"})
    now = datetime.now(UTC)
    subscription = await db.scalar(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.status.in_(["active", "grace"]))
        .order_by(Subscription.created_at.desc())
    )
    paid_through = subscription.paid_through if subscription and subscription.paid_through else None
    if paid_through and paid_through.tzinfo is None:
        paid_through = paid_through.replace(tzinfo=UTC)
    starts_at = max(now, paid_through) if paid_through else now
    grant = ComplimentaryGrant(
        user_id=user_id,
        plan=payload.plan,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=30),
        reason=payload.reason,
        granted_by_user_id=admin.id,
    )
    db.add(grant)
    db.add(
        EntitlementAuditEvent(
            user_id=user_id,
            actor_user_id=admin.id,
            action="complimentary_access_granted",
            reason=payload.reason,
            details={"plan": payload.plan, "starts_at": starts_at.isoformat(), "ends_at": grant.ends_at.isoformat()},
        )
    )
    if subscription:
        subscription.cancel_at_period_end = True
        subscription.cancelled_at = utcnow()
    await db.commit()
    if subscription and subscription.paystack_subscription_code and subscription.paystack_email_token:
        try:
            await PaystackClient().disable_subscription(subscription.paystack_subscription_code, subscription.paystack_email_token)
        except PaystackError:
            pass
    await publish_realtime_event(user_id, "billing.updated")
    return {"grant_id": grant.id, "starts_at": grant.starts_at, "ends_at": grant.ends_at, "plan": grant.plan}


@router.post("/users/{user_id}/trial-reset")
async def reset_trial(
    user_id: str,
    payload: TrialResetIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if not await db.get(User, user_id):
        raise HTTPException(status_code=404, detail={"code": "user_not_found", "message": "Creator not found"})
    await db.execute(delete(TrialUsage).where(TrialUsage.user_id == user_id))
    db.add(
        EntitlementAuditEvent(
            user_id=user_id,
            actor_user_id=admin.id,
            action="trial_reset",
            reason=payload.reason,
            details={},
        )
    )
    await db.commit()
    await publish_realtime_event(user_id, "billing.updated")
    return {"message": "Trial preview restored"}


@router.patch("/features/ai")
async def update_ai_feature(
    payload: AIFeatureIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    flag = await db.get(FeatureFlag, "ai")
    if not flag:
        flag = FeatureFlag(key="ai")
        db.add(flag)
    flag.enabled = payload.enabled
    flag.updated_by_user_id = admin.id
    db.add(
        EntitlementAuditEvent(
            user_id=admin.id,
            actor_user_id=admin.id,
            action="ai_feature_updated",
            reason=payload.reason,
            details={"enabled": payload.enabled},
        )
    )
    await db.commit()
    return {"enabled": flag.enabled}
