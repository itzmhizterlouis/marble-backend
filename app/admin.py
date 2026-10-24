from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db
from .deps import get_current_user
from .entitlements import TRIAL_DAYS, TRIAL_EXTENSION_DAYS, resolve_billing_access
from .events import publish_realtime_event
from .models import (
    ComplimentaryGrant,
    EntitlementAuditEvent,
    FeatureFlag,
    Post,
    Publication,
    SocialConnection,
    Subscription,
    User,
    utcnow,
)
from .paystack import PaystackClient, PaystackError
from .providers import ProviderError, UploadPostClient

ADMIN_USERS_PAGE_SIZE = 20
CONNECTED_STATUSES = ("connected", "needs_selection")

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class GrantIn(BaseModel):
    plan: str = Field(pattern="^(basic|pro)$")
    reason: str = Field(min_length=5, max_length=1000)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


class TrialExtensionIn(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)
    dry_run: bool = True


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
    page: int = Query(default=1, ge=1, le=100000),
    has_posts: bool = Query(default=False),
    has_connections: bool = Query(default=False),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    del admin
    query = (
        select(User, func.count(Post.id).label("post_count"))
        .outerjoin(Post, Post.user_id == User.id)
        .group_by(User.id)
    )
    if q.strip():
        search = f"%{q.strip()}%"
        query = query.where(or_(User.email.ilike(search), User.name.ilike(search)))
    if has_posts:
        query = query.having(func.count(Post.id) > 0)
    if has_connections:
        query = query.where(
            select(SocialConnection.id)
            .where(
                SocialConnection.user_id == User.id,
                SocialConnection.status.in_(CONNECTED_STATUSES),
            )
            .exists()
        )
    matching = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, -(-matching // ADMIN_USERS_PAGE_SIZE))
    page = min(page, pages)
    users = (
        await db.execute(
            query.order_by(User.created_at.desc(), User.id)
            .limit(ADMIN_USERS_PAGE_SIZE)
            .offset((page - 1) * ADMIN_USERS_PAGE_SIZE)
        )
    ).all()
    connected: dict[str, list[str]] = {}
    if users:
        rows = await db.execute(
            select(SocialConnection.user_id, SocialConnection.platform)
            .where(
                SocialConnection.user_id.in_([user.id for user, _ in users]),
                SocialConnection.status.in_(CONNECTED_STATUSES),
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
        "matching_users": matching,
        "page": page,
        "pages": pages,
        "page_size": ADMIN_USERS_PAGE_SIZE,
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


@router.get("/views-summary")
async def platform_views_summary(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Latest lifetime views across every creator, using the same rules as each creator's Home total."""
    del admin
    from .analytics import (
        ANALYTICS_PLATFORMS,
        latest_publication_snapshots,
        summary_metric,
        usable_view_result,
    )

    publications = list(
        await db.scalars(
            select(Publication).where(
                Publication.status == "published", Publication.platform.in_(ANALYTICS_PLATFORMS)
            )
        )
    )
    ids = [publication.id for publication in publications]
    latest: dict = {}
    successful: dict = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        latest.update(await latest_publication_snapshots(db, chunk))
        successful.update(await latest_publication_snapshots(db, chunk, successful=True))
    per_platform: dict[str, dict] = {}
    for publication in publications:
        result = usable_view_result(publication, latest.get(publication.id), successful.get(publication.id))
        if result is None:
            continue
        entry = per_platform.setdefault(
            publication.platform, {"platform": publication.platform, "views": 0, "posts": 0}
        )
        entry["views"] += result["metrics"][summary_metric(publication.platform)]
        entry["posts"] += 1
    platforms = sorted(per_platform.values(), key=lambda item: item["platform"])
    creators = await db.scalar(select(func.count(func.distinct(Post.user_id)))) or 0
    return {
        "total_views": sum(item["views"] for item in platforms),
        "counted_posts": sum(item["posts"] for item in platforms),
        "published_destinations": len(publications),
        "creators_with_posts": creators,
        "platforms": platforms,
    }


@router.post("/trial-extension")
async def extend_all_trials(
    payload: TrialExtensionIn,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Give every verified trial creator who never paid (and was not extended yet) more Pro days."""
    now = utcnow()
    users = list(
        await db.scalars(
            select(User).where(
                User.email_verified.is_(True),
                User.trial_ends_at.is_not(None),
                User.trial_extended_at.is_(None),
                ~select(Subscription.id).where(Subscription.user_id == User.id).exists(),
            )
        )
    )
    extension = timedelta(days=TRIAL_EXTENSION_DAYS)
    active = expired = 0
    changes: list[tuple[User, datetime, datetime]] = []
    for user in users:
        ends = user.trial_ends_at if user.trial_ends_at.tzinfo else user.trial_ends_at.replace(tzinfo=UTC)
        if ends > now:
            active += 1
            new_end = ends + extension
        else:
            expired += 1
            new_end = now + extension
        changes.append((user, ends, new_end))
    summary = {"extra_days": TRIAL_EXTENSION_DAYS, "active": active, "expired": expired, "total": len(changes)}
    if payload.dry_run:
        return {**summary, "applied": False}
    for user, old_end, new_end in changes:
        user.trial_ends_at = new_end
        user.trial_extended_at = now
        db.add(
            EntitlementAuditEvent(
                user_id=user.id,
                actor_user_id=admin.id,
                action="trial_extended_bulk",
                reason=payload.reason,
                details={"previous_trial_ends_at": old_end.isoformat(), "trial_ends_at": new_end.isoformat(), "extra_days": TRIAL_EXTENSION_DAYS},
            )
        )
    await db.commit()
    if changes:
        from .tasks import send_trial_extension_emails

        send_trial_extension_emails.delay()
    return {**summary, "applied": True}


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
