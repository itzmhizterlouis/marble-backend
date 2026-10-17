from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db
from .deps import get_current_user
from .entitlements import TRIAL_DAYS, resolve_billing_access
from .events import publish_realtime_event
from .models import (
    ComplimentaryGrant,
    EntitlementAuditEvent,
    FeatureFlag,
    Post,
    SocialConnection,
    Subscription,
    User,
    utcnow,
)
from .paystack import PaystackClient, PaystackError
from .providers import ProviderError, UploadPostClient

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class GrantIn(BaseModel):
    plan: str = Field(pattern="^(basic|pro)$")
    reason: str = Field(min_length=5, max_length=1000)


class ReasonIn(BaseModel):
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
    query = (
        select(User, func.count(Post.id).label("post_count"))
        .outerjoin(Post, Post.user_id == User.id)
        .group_by(User.id)
        .order_by(User.created_at.desc())
        .limit(100)
    )
    if q.strip():
        search = f"%{q.strip()}%"
        query = query.where(or_(User.email.ilike(search), User.name.ilike(search)))
    users = (await db.execute(query)).all()
    connected: dict[str, list[str]] = {}
    if users:
        rows = await db.execute(
            select(SocialConnection.user_id, SocialConnection.platform)
            .where(
                SocialConnection.user_id.in_([user.id for user, _ in users]),
                SocialConnection.status.in_(["connected", "needs_selection"]),
            )
            .order_by(SocialConnection.platform)
        )
        for user_id, platform in rows.all():
            connected.setdefault(user_id, []).append(platform)
    totals = (
        await db.execute(
            select(func.count(User.id), func.count(User.id).filter(User.email_verified.is_(True)))
        )
    ).one()
    return {
        "total_users": totals[0],
        "verified_users": totals[1],
        "items": [
            {
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "email_verified": user.email_verified,
                "created_at": user.created_at,
                "post_count": post_count,
                "connected_platforms": connected.get(user.id, []),
                "billing": (await resolve_billing_access(db, user)).payload(),
            }
            for user, post_count in users
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
    payload: ReasonIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail={"code": "user_not_found", "message": "Creator not found"})
    user.trial_ends_at = utcnow() + timedelta(days=TRIAL_DAYS)
    db.add(
        EntitlementAuditEvent(
            user_id=user_id,
            actor_user_id=admin.id,
            action="trial_reset",
            reason=payload.reason,
            details={"trial_ends_at": user.trial_ends_at.isoformat()},
        )
    )
    await db.commit()
    await publish_realtime_event(user_id, "billing.updated")
    return {"message": f"Pro trial restarted for {TRIAL_DAYS} days"}


@router.post("/users/{user_id}/upload-post-profile/delete")
async def delete_upload_post_profile(
    user_id: str,
    payload: ReasonIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail={"code": "user_not_found", "message": "Creator not found"})
    active = await db.scalar(
        select(func.count(Post.id)).where(
            Post.user_id == user_id, Post.status.in_(["scheduled", "queued", "publishing"])
        )
    )
    if active:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "profile_in_use",
                "message": f"This creator has {active} scheduled or posting {'post' if active == 1 else 'posts'}. Cancel or let them finish first.",
            },
        )
    already_deleted = False
    try:
        await UploadPostClient().delete_profile(user.upload_post_profile)
    except ProviderError as exc:
        if exc.status_code != 404:
            raise HTTPException(
                status_code=502 if exc.status_code < 500 else exc.status_code,
                detail={"code": "profile_delete_failed", "message": f"Upload-Post did not delete the profile: {exc}"},
            ) from exc
        already_deleted = True
    connections = list(await db.scalars(select(SocialConnection).where(SocialConnection.user_id == user_id)))
    for row in connections:
        row.status = "disconnected"
        row.reauth_required = False
        row.provider_account_id = None
        row.username = row.handle = row.display_name = row.avatar_url = None
        row.capabilities = []
        row.connected_at = None
        row.target_page_id = None
    db.add(
        EntitlementAuditEvent(
            user_id=user_id,
            actor_user_id=admin.id,
            action="upload_post_profile_deleted",
            reason=payload.reason,
            details={"profile": user.upload_post_profile, "already_deleted": already_deleted},
        )
    )
    await db.commit()
    await publish_realtime_event(user_id, "connection.updated")
    return {
        "message": "Upload-Post profile was already gone; connections cleared"
        if already_deleted
        else "Upload-Post profile deleted and connections cleared"
    }


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
