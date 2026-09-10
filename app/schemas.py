from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

Platform = Literal["tiktok", "instagram", "youtube", "facebook"]


class UserOut(BaseModel):
    id: str
    email: EmailStr
    name: str
    email_verified: bool

    model_config = {"from_attributes": True}


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class EmailIn(BaseModel):
    email: EmailStr


class TokenIn(BaseModel):
    token: str


class PasswordResetIn(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=128)


class MessageOut(BaseModel):
    message: str


class BulkDeleteIn(BaseModel):
    post_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("post_ids")
    @classmethod
    def deduplicate_post_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class BulkDeleteFailureOut(BaseModel):
    post_id: str
    code: str
    message: str


class BulkDeleteOut(BaseModel):
    deleted_ids: list[str]
    failures: list[BulkDeleteFailureOut]


class ConnectionOut(BaseModel):
    platform: Platform
    status: str
    provider: Literal["upload_post"] = "upload_post"
    username: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    capabilities: list[str] = []
    reauth_required: bool = False
    target_page_id: str | None = None
    connected_at: datetime | None = None
    last_used_at: datetime | None = None


class AuthorizeOut(BaseModel):
    authorize_url: str


class FacebookPageOut(BaseModel):
    id: str
    name: str


class FacebookPageSelection(BaseModel):
    page_id: str


class MediaInitIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: Literal["video/mp4", "video/quicktime"]
    size_bytes: int = Field(gt=0)


class MediaPartConfirmIn(BaseModel):
    part_number: int = Field(ge=1, le=10_000)
    etag: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)


class MediaPartUrlOut(BaseModel):
    part_number: int
    url: str
    expires_at: datetime


class MediaOut(BaseModel):
    id: str
    original_name: str
    mime_type: str
    size_bytes: int
    uploaded_bytes: int
    duration_seconds: int | None
    width: int | None
    height: int | None
    status: str
    thumbnail_url: str | None = None
    chunk_size: int | None = None
    upload_mode: Literal["local", "r2"] = "local"


class VersionIn(BaseModel):
    platform: Platform
    caption: str = Field(default="", max_length=63206)
    title: str | None = Field(default=None, max_length=255)
    options: dict = Field(default_factory=dict)


class PostUpsertIn(BaseModel):
    media_id: str
    title: str = Field(default="", max_length=255)
    caption: str = Field(default="", max_length=63206)
    hashtags: list[str] = Field(default_factory=list)
    versions: list[VersionIn] = Field(default_factory=list)


class PublishIn(BaseModel):
    mode: Literal["now", "scheduled"]
    scheduled_at: datetime | None = None
    timezone: str | None = None

    @field_validator("scheduled_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("scheduled_at must include a timezone offset")
        return value


class ScheduleUpdateIn(BaseModel):
    scheduled_at: datetime | None = None
    timezone: str | None = None
    title: str | None = Field(default=None, max_length=255)
    caption: str | None = Field(default=None, max_length=63206)
    hashtags: list[str] | None = None
    versions: list[VersionIn] | None = None


class PublicationOut(BaseModel):
    platform: Platform
    status: str
    attempts: int
    url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    fallback_to_inbox: bool = False
    published_at: datetime | None = None


class PostOut(BaseModel):
    id: str
    media: MediaOut
    title: str
    caption: str
    hashtags: list[str]
    status: str
    publish_mode: str | None
    scheduled_at: datetime | None
    schedule_timezone: str | None
    provider_job_id: str | None
    can_edit_schedule: bool
    versions: list[VersionIn]
    publications: list[PublicationOut]
    created_at: datetime
    updated_at: datetime


class PostListOut(BaseModel):
    items: list[PostOut]
    next_cursor: str | None = None


class ErrorOut(BaseModel):
    code: str
    message: str
    field_errors: dict | None = None
    request_id: str | None = None
