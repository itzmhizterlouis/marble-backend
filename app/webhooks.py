import hashlib
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .database import get_db
from .events import publish_post_event
from .models import Post, Publication, PublicationAttempt, SocialConnection, User, WebhookEvent
from .notifications import queue_terminal_notification
from .providers import verify_upload_post_signature
from .schemas import MessageOut
from .tasks import apply_provider_payload

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post("/upload-post", response_model=MessageOut)
async def upload_post_webhook(
    request: Request,
    signature: str | None = Header(default=None, alias="X-Upload-Post-Signature"),
    timestamp: str | None = Header(default=None, alias="X-Upload-Post-Timestamp"),
    delivery_id: str | None = Header(default=None, alias="X-Upload-Post-Delivery"),
    db: AsyncSession = Depends(get_db),
):
    body = await request.body()
    if not verify_upload_post_signature(body, signature, timestamp):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_webhook_signature", "message": "Invalid webhook signature"},
        )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_webhook_payload", "message": "Webhook body must be valid JSON"},
        ) from exc
    event_id = str(
        delivery_id
        or payload.get("event_id")
        or payload.get("id")
        or hashlib.sha256(body).hexdigest()
    )
    if await db.scalar(select(WebhookEvent.id).where(WebhookEvent.provider_event_id == event_id)):
        return MessageOut(message="Already processed")
    event = WebhookEvent(provider_event_id=event_id, payload=payload)
    db.add(event)
    event_name = str(payload.get("event") or "")
    profile_username = payload.get("profile_username")
    platform = payload.get("platform")
    if event_name in {
        "social_account_connected",
        "social_account_disconnected",
        "social_account_reauth_required",
    } and profile_username and platform in {"tiktok", "instagram", "youtube", "facebook"}:
        user = await db.scalar(select(User).where(User.upload_post_profile == profile_username))
        if user:
            connection = await db.scalar(
                select(SocialConnection).where(
                    SocialConnection.user_id == user.id,
                    SocialConnection.platform == platform,
                )
            )
            if not connection:
                connection = SocialConnection(user_id=user.id, platform=platform)
                db.add(connection)
            connection.status = {
                "social_account_connected": "connected",
                "social_account_disconnected": "disconnected",
                "social_account_reauth_required": "expired",
            }[event_name]
            connection.reauth_required = event_name == "social_account_reauth_required"
            connection.last_synced_at = datetime.now(UTC)
            if event_name == "social_account_disconnected":
                connection.provider_account_id = None
                connection.username = None
                connection.handle = None
                connection.display_name = None
                connection.avatar_url = None
                connection.capabilities = []
                connection.target_page_id = None
            elif payload.get("account_name"):
                # Upload-Post documents account_name as the platform identifier
                # (for example a YouTube channel ID), not the public @handle.
                identifier = str(payload["account_name"]).strip()
                if identifier:
                    connection.provider_account_id = identifier
                    connection.username = identifier
    external_id = str(payload.get("external_id") or "").split(":")[0]
    request_id = payload.get("request_id")
    job_id = payload.get("job_id")
    clauses = []
    if external_id:
        clauses.append(Post.id == external_id)
    if request_id:
        clauses.append(Post.provider_request_id == request_id)
        clauses.append(Post.publications.any(Publication.provider_request_id == request_id))
        clauses.append(
            Post.publications.any(
                Publication.provider_attempts.any(
                    PublicationAttempt.provider_request_id == request_id
                )
            )
        )
    if job_id:
        clauses.append(Post.provider_job_id == job_id)
        clauses.append(Post.publications.any(Publication.provider_job_id == job_id))
        clauses.append(
            Post.publications.any(
                Publication.provider_attempts.any(PublicationAttempt.provider_job_id == job_id)
            )
        )
    post = None
    if clauses:
        post = await db.scalar(
            select(Post)
            .where(or_(*clauses))
            .options(selectinload(Post.media), selectinload(Post.publications))
        )
    if post:
        await apply_provider_payload(post, payload)
        await queue_terminal_notification(db, post)
    event.processed_at = datetime.now(UTC)
    await db.commit()
    if post:
        await publish_post_event(post.user_id, post.id)
    return MessageOut(message="Processed")
