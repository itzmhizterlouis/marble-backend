from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite+aiosqlite:///./marble.db"
    redis_url: str = "redis://localhost:6379/0"
    frontend_url: str = "http://localhost:4173"
    api_public_url: str = "http://localhost:8000"
    jwt_secret: str = "development-secret-change-before-production-123"
    token_encryption_key: str = "development-encryption-change-before-prod"
    upload_post_api_key: str = ""
    upload_post_base_url: str = "https://api.upload-post.com/api"
    upload_post_webhook_secret: str = ""
    brevo_api_key: str = ""
    brevo_sender_email: str = ""
    brevo_sender_name: str = "Reverb"
    publish_failure_emails_enabled: bool = True
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/v1/auth/google/callback"
    storage_backend: Literal["local", "r2"] = "local"
    storage_root: Path = Path("./data")
    r2_endpoint: str = ""
    r2_bucket_name: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_region: str = "auto"
    r2_presign_ttl_seconds: int = 900
    r2_multipart_part_size_bytes: int = 8 * 1024 * 1024
    max_upload_bytes: int = 500 * 1024 * 1024
    upload_chunk_bytes: int = 2 * 1024 * 1024
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    paystack_secret_key: str = ""
    paystack_basic_plan_code: str = ""
    paystack_pro_plan_code: str = ""
    paystack_callback_url: str = "http://localhost:4173/billing/callback"
    admin_emails: str = ""
    billing_enforcement_enabled: bool = False
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    ai_enabled: bool = True
    ai_video_daily_limit: int = 20
    ai_adjustment_daily_limit: int = 100
    ai_global_video_daily_limit: int = 1000
    ai_global_adjustment_daily_limit: int = 5000

    @field_validator("frontend_url", "api_public_url", "paystack_callback_url", mode="before")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_driver(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://") and "+asyncpg" not in value:
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @field_validator("r2_endpoint", mode="before")
    @classmethod
    def strip_r2_endpoint_slash(cls, value: str) -> str:
        return (value or "").rstrip("/")

    @model_validator(mode="after")
    def validate_production_configuration(self):
        if self.r2_multipart_part_size_bytes < 5 * 1024 * 1024:
            raise ValueError("R2_MULTIPART_PART_SIZE_BYTES must be at least 5 MiB")
        if not 1 <= self.r2_presign_ttl_seconds <= 604800:
            raise ValueError("R2_PRESIGN_TTL_SECONDS must be between 1 second and 7 days")
        if not self.is_production:
            return self
        required = {
            "UPLOAD_POST_API_KEY": self.upload_post_api_key,
            "UPLOAD_POST_WEBHOOK_SECRET": self.upload_post_webhook_secret,
            "BREVO_API_KEY": self.brevo_api_key,
            "BREVO_SENDER_EMAIL": self.brevo_sender_email,
            "GOOGLE_CLIENT_ID": self.google_client_id,
            "GOOGLE_CLIENT_SECRET": self.google_client_secret,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing production settings: {', '.join(missing)}")
        if self.jwt_secret.startswith("development-") or len(self.jwt_secret) < 32:
            raise ValueError("JWT_SECRET must be a production secret of at least 32 characters")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("Production DATABASE_URL must point to PostgreSQL")
        if self.storage_backend == "r2":
            r2_required = {
                "R2_ENDPOINT": self.r2_endpoint,
                "R2_BUCKET_NAME": self.r2_bucket_name,
                "R2_ACCESS_KEY_ID": self.r2_access_key_id,
                "R2_SECRET_ACCESS_KEY": self.r2_secret_access_key,
            }
            missing_r2 = [name for name, value in r2_required.items() if not value]
            if missing_r2:
                raise ValueError(f"Missing production settings: {', '.join(missing_r2)}")
        if self.billing_enforcement_enabled:
            billing_required = {
                "PAYSTACK_SECRET_KEY": self.paystack_secret_key,
                "PAYSTACK_BASIC_PLAN_CODE": self.paystack_basic_plan_code,
                "PAYSTACK_PRO_PLAN_CODE": self.paystack_pro_plan_code,
                "PAYSTACK_CALLBACK_URL": self.paystack_callback_url,
            }
            missing_billing = [name for name, value in billing_required.items() if not value]
            if missing_billing:
                raise ValueError(f"Missing production settings: {', '.join(missing_billing)}")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def admin_email_set(self) -> set[str]:
        return {email.strip().casefold() for email in self.admin_emails.split(",") if email.strip()}

    def ensure_storage(self) -> None:
        (self.storage_root / "uploads").mkdir(parents=True, exist_ok=True)
        (self.storage_root / "thumbnails").mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_storage()
    return settings
