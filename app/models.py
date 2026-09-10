from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    upload_post_profile: Mapped[str] = mapped_column(String(80), unique=True)

    sessions: Mapped[list[AuthSession]] = relationship(back_populates="user", cascade="all, delete-orphan")
    connections: Mapped[list[SocialConnection]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    media: Mapped[list[MediaAsset]] = relationship(back_populates="user", cascade="all, delete-orphan")
    posts: Mapped[list[Post]] = relationship(back_populates="user", cascade="all, delete-orphan")


class AuthSession(Base, TimestampMixin):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class OneTimeToken(Base):
    __tablename__ = "one_time_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(32), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SocialConnection(Base, TimestampMixin):
    __tablename__ = "social_connections"
    __table_args__ = (UniqueConstraint("user_id", "platform", name="uq_connection_user_platform"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(32), default="disconnected")
    provider_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    reauth_required: Mapped[bool] = mapped_column(Boolean, default=False)
    target_page_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="connections")


class MediaAsset(Base, TimestampMixin):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    uploaded_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_path: Mapped[str] = mapped_column(Text)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_backend: Mapped[str] = mapped_column(String(16), default="local", index=True)
    object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    multipart_upload_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="uploading")
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="media")
    posts: Mapped[list[Post]] = relationship(back_populates="media")
    upload_parts: Mapped[list[MediaUploadPart]] = relationship(
        back_populates="media",
        cascade="all, delete-orphan",
        order_by="MediaUploadPart.part_number",
    )


class MediaUploadPart(Base, TimestampMixin):
    __tablename__ = "media_upload_parts"
    __table_args__ = (PrimaryKeyConstraint("media_id", "part_number"),)

    media_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id", ondelete="CASCADE"))
    part_number: Mapped[int] = mapped_column(Integer)
    etag: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)

    media: Mapped[MediaAsset] = relationship(back_populates="upload_parts")


class Post(Base, TimestampMixin):
    __tablename__ = "posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    media_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id", ondelete="RESTRICT"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    caption: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    publish_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    scheduled_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    schedule_timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    provider_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    schedule_revision: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship(back_populates="posts")
    media: Mapped[MediaAsset] = relationship(back_populates="posts")
    versions: Mapped[list[PlatformVersion]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
    publications: Mapped[list[Publication]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
    notifications: Mapped[list[PostNotification]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )


class PlatformVersion(Base, TimestampMixin):
    __tablename__ = "platform_versions"
    __table_args__ = (UniqueConstraint("post_id", "platform", name="uq_version_post_platform"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(24))
    caption: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    options: Mapped[dict] = mapped_column(JSON, default=dict)

    post: Mapped[Post] = relationship(back_populates="versions")


class Publication(Base, TimestampMixin):
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("post_id", "platform", name="uq_publication_post_platform"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    provider_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    provider_post_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_to_inbox: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    post: Mapped[Post] = relationship(back_populates="publications")
    provider_attempts: Mapped[list[PublicationAttempt]] = relationship(
        back_populates="publication", cascade="all, delete-orphan"
    )


class PublicationAttempt(Base, TimestampMixin):
    __tablename__ = "publication_attempts"
    __table_args__ = (
        UniqueConstraint("publication_id", "idempotency_key", name="uq_attempt_publication_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    publication_id: Mapped[str] = mapped_column(
        ForeignKey("publications.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="submitted")
    provider_request_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    provider_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255))

    publication: Mapped[Publication] = relationship(back_populates="provider_attempts")


class PostNotification(Base, TimestampMixin):
    __tablename__ = "post_notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    publish_revision: Mapped[int] = mapped_column(Integer)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    recipient_email: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    post: Mapped[Post] = relationship(back_populates="notifications")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_event_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
