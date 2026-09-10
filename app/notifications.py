from __future__ import annotations

import logging
from datetime import timedelta
from html import escape

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .config import get_settings
from .email import send_transactional_email
from .models import Post, PostNotification, User, utcnow

logger = logging.getLogger(__name__)
MAX_DELIVERY_ATTEMPTS = 8


def terminal_notification_kind(post: Post) -> str | None:
    statuses = {publication.status for publication in post.publications}
    if not statuses or statuses & {"scheduled", "queued", "publishing"}:
        return None
    if "failed" in statuses:
        return "publish_failed"
    if "action_required" in statuses:
        return "action_required"
    return None


async def queue_terminal_notification(db: AsyncSession, post: Post) -> bool:
    settings = get_settings()
    if not settings.publish_failure_emails_enabled:
        return False
    kind = terminal_notification_kind(post)
    if not kind:
        return False
    user = await db.get(User, post.user_id)
    if not user:
        return False
    dedupe_key = f"{post.id}:{kind}:{post.schedule_revision}"
    values = {
        "post_id": post.id,
        "kind": kind,
        "publish_revision": post.schedule_revision,
        "dedupe_key": dedupe_key,
        "recipient_email": user.email,
        "status": "pending",
        "attempts": 0,
        "next_attempt_at": utcnow(),
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(PostNotification).values(**values).on_conflict_do_nothing(
            index_elements=["dedupe_key"]
        )
    elif dialect == "sqlite":
        statement = sqlite_insert(PostNotification).values(**values).on_conflict_do_nothing(
            index_elements=["dedupe_key"]
        )
    else:
        if await db.scalar(
            select(PostNotification.id).where(PostNotification.dedupe_key == dedupe_key)
        ):
            return False
        db.add(PostNotification(**values))
        return True
    result = await db.execute(statement)
    return bool(result.rowcount)


def render_post_notification(post: Post, kind: str) -> tuple[str, str]:
    failed = [item for item in post.publications if item.status == "failed"]
    published = [item for item in post.publications if item.status == "published"]
    action_required = [item for item in post.publications if item.status == "action_required"]
    if kind == "action_required":
        subject = "Action required for your Reverb post"
        heading = "Your post needs one more step"
        intro = "A destination needs your attention before your post can go live."
    elif published:
        subject = "Some destinations need attention"
        heading = "Part of your Reverb post failed"
        intro = "Your successful destinations are still live. You can review and retry only the ones that failed."
    else:
        subject = "Your Reverb post couldn’t be published"
        heading = "Your post didn’t go live"
        intro = "We couldn’t publish this post. Review the details and retry when you’re ready."

    rows = []
    for publication in sorted(post.publications, key=lambda item: item.platform):
        platform = escape(publication.platform.replace("_", " ").title())
        label = {
            "published": "Published",
            "failed": "Failed",
            "action_required": "Action required",
        }.get(publication.status, publication.status.replace("_", " ").title())
        detail = ""
        if publication in failed and publication.error_message:
            detail = f'<div style="color:#695f5f;font-size:13px;margin-top:3px">{escape(publication.error_message)}</div>'
        rows.append(
            '<li style="padding:10px 0;border-bottom:1px solid #eee7df">'
            f'<strong>{platform}</strong><span style="float:right">{escape(label)}</span>{detail}</li>'
        )
    post_url = f"{get_settings().frontend_url}/content/{post.id}"
    action_note = ""
    if action_required:
        action_note = "<p>Open Reverb to complete any required action for the destination.</p>"
    html = f"""
    <div style="background:#f6f1e9;padding:28px 16px;font-family:Arial,sans-serif;color:#211d1d">
      <div style="max-width:560px;margin:auto;background:#fff;border-radius:18px;padding:30px">
        <div style="font-size:20px;font-weight:700;color:#751f36;margin-bottom:24px">Reverb</div>
        <h1 style="font-size:24px;line-height:1.25;margin:0 0 12px">{heading}</h1>
        <p style="line-height:1.6;color:#554d4d">{intro}</p>
        <ul style="list-style:none;padding:0;margin:22px 0">{''.join(rows)}</ul>
        {action_note}
        <a href="{escape(post_url, quote=True)}" style="display:inline-block;background:#751f36;color:#fff;text-decoration:none;padding:12px 18px;border-radius:999px;font-weight:700">View post</a>
        <p style="font-size:12px;color:#857b76;margin-top:26px">You’re receiving this because you published a post with Reverb.</p>
      </div>
    </div>
    """
    return subject, html


async def deliver_pending_notifications(db: AsyncSession, limit: int = 20) -> int:
    now = utcnow()
    notifications = list(
        await db.scalars(
            select(PostNotification)
            .where(
                PostNotification.status == "pending",
                PostNotification.next_attempt_at <= now,
            )
            .options(
                selectinload(PostNotification.post).selectinload(Post.user),
                selectinload(PostNotification.post).selectinload(Post.publications),
            )
            .order_by(PostNotification.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    delivered = 0
    for notification in notifications:
        post = notification.post
        # A quick retry may have superseded a pending failure email.
        if (
            notification.publish_revision != post.schedule_revision
            or terminal_notification_kind(post) != notification.kind
        ):
            notification.status = "cancelled"
            continue
        subject, html = render_post_notification(post, notification.kind)
        try:
            notification.provider_message_id = await send_transactional_email(
                notification.recipient_email, post.user.name, subject, html
            )
        except Exception as exc:  # Delivery failures must never change publication state.
            notification.attempts += 1
            notification.last_error = str(exc)[:2000]
            if notification.attempts >= MAX_DELIVERY_ATTEMPTS:
                notification.status = "failed"
            else:
                delay_minutes = min(2 ** (notification.attempts - 1), 60)
                notification.next_attempt_at = now + timedelta(minutes=delay_minutes)
            logger.warning("Could not deliver post notification %s: %s", notification.id, exc)
        else:
            notification.status = "sent"
            notification.sent_at = utcnow()
            notification.last_error = None
            delivered += 1
    await db.commit()
    return delivered
