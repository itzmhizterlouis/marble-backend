import asyncio

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models import MediaAsset, Post, PostNotification, Publication, User
from app.notifications import deliver_pending_notifications, queue_terminal_notification


async def create_failed_post(email: str, statuses: tuple[str, ...] = ("failed",)) -> str:
    async with SessionLocal() as db:
        user = User(
            email=email,
            name="Notification Creator",
            email_verified=True,
            upload_post_profile=f"notify_{email.split('@')[0]}",
        )
        media = MediaAsset(
            user=user,
            original_name="launch.mp4",
            mime_type="video/mp4",
            size_bytes=100,
            uploaded_bytes=100,
            storage_path="/tmp/launch.mp4",
            status="ready",
        )
        post = Post(
            user=user,
            media=media,
            title="Launch",
            status="failed",
            publish_mode="now",
            schedule_revision=1,
        )
        platforms = ("instagram", "youtube", "facebook")
        for platform, status in zip(platforms, statuses, strict=False):
            post.publications.append(
                Publication(
                    platform=platform,
                    status=status,
                    error_message="Provider rejected <unsafe>" if status == "failed" else None,
                )
            )
        db.add(post)
        await db.commit()
        return post.id


async def load_post(post_id: str) -> Post:
    async with SessionLocal() as db:
        return await db.scalar(
            select(Post)
            .where(Post.id == post_id)
            .options(selectinload(Post.publications))
        )


def test_terminal_notification_is_deduplicated(client):
    post_id = asyncio.run(create_failed_post("dedupe@example.com"))

    async def queue_twice():
        async with SessionLocal() as db:
            post = await db.scalar(
                select(Post)
                .where(Post.id == post_id)
                .options(selectinload(Post.publications))
            )
            await queue_terminal_notification(db, post)
            await queue_terminal_notification(db, post)
            await db.commit()
        async with SessionLocal() as db:
            count = await db.scalar(
                select(func.count(PostNotification.id)).where(PostNotification.post_id == post_id)
            )
            notification = await db.scalar(
                select(PostNotification).where(PostNotification.post_id == post_id)
            )
            notification.status = "cancelled"
            await db.commit()
            return count

    assert asyncio.run(queue_twice()) == 1


def test_working_destination_does_not_queue_email(client):
    post_id = asyncio.run(create_failed_post("working@example.com", ("failed", "publishing")))

    async def queue_and_count():
        async with SessionLocal() as db:
            post = await db.scalar(
                select(Post)
                .where(Post.id == post_id)
                .options(selectinload(Post.publications))
            )
            assert await queue_terminal_notification(db, post) is False
            await db.commit()
            return await db.scalar(
                select(func.count(PostNotification.id)).where(PostNotification.post_id == post_id)
            )

    assert asyncio.run(queue_and_count()) == 0


def test_pending_notification_is_delivered_and_escaped(client, monkeypatch):
    post_id = asyncio.run(
        create_failed_post("delivery@example.com", ("published", "failed"))
    )
    delivered = []

    async def capture_email(email, name, subject, html):
        delivered.append((email, name, subject, html))
        return "brevo-message-id"

    monkeypatch.setattr("app.notifications.send_transactional_email", capture_email)

    async def queue_and_deliver():
        async with SessionLocal() as db:
            post = await db.scalar(
                select(Post)
                .where(Post.id == post_id)
                .options(selectinload(Post.publications))
            )
            await queue_terminal_notification(db, post)
            await db.commit()
        async with SessionLocal() as db:
            assert await deliver_pending_notifications(db) == 1
        async with SessionLocal() as db:
            return await db.scalar(
                select(PostNotification).where(PostNotification.post_id == post_id)
            )

    notification = asyncio.run(queue_and_deliver())
    assert notification.status == "sent"
    assert notification.provider_message_id == "brevo-message-id"
    assert delivered[0][0] == "delivery@example.com"
    assert delivered[0][2] == "Some destinations need attention"
    assert f"/content/{post_id}" in delivered[0][3]
    assert "&lt;unsafe&gt;" in delivered[0][3]
    assert "<unsafe>" not in delivered[0][3]


def test_retry_revision_cancels_stale_pending_email(client, monkeypatch):
    post_id = asyncio.run(create_failed_post("stale@example.com"))
    delivered = []

    async def capture_email(*args):
        delivered.append(args)

    monkeypatch.setattr("app.notifications.send_transactional_email", capture_email)

    async def queue_then_retry():
        async with SessionLocal() as db:
            post = await db.scalar(
                select(Post)
                .where(Post.id == post_id)
                .options(selectinload(Post.publications))
            )
            await queue_terminal_notification(db, post)
            await db.commit()
            post.schedule_revision += 1
            post.publications[0].status = "queued"
            await db.commit()
        async with SessionLocal() as db:
            assert await deliver_pending_notifications(db) == 0
        async with SessionLocal() as db:
            return await db.scalar(
                select(PostNotification).where(PostNotification.post_id == post_id)
            )

    notification = asyncio.run(queue_then_retry())
    assert notification.status == "cancelled"
    assert delivered == []
