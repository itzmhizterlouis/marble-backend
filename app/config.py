from functools import lru_cache
from pathlib import Path

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
    storage_root: Path = Path("./data")
    max_upload_bytes: int = 500 * 1024 * 1024
    upload_chunk_bytes: int = 2 * 1024 * 1024
    access_token_minutes: int = 15
    refresh_token_days: int = 30

    @field_validator("frontend_url", "api_public_url", mode="before")
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

    @model_validator(mode="after")
    def validate_production_configuration(self):
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
        return self

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    def ensure_storage(self) -> None:
        (self.storage_root / "uploads").mkdir(parents=True, exist_ok=True)
        (self.storage_root / "thumbnails").mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_storage()
    return settings
