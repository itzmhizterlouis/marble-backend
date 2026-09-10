import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import get_settings

password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        return password_hasher.verify(encoded, password)
    except VerifyMismatchError:
        return False


def create_access_token(user_id: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "type": "access",
            "jti": secrets.token_urlsafe(12),
            "iat": now,
            "exp": now + timedelta(minutes=settings.access_token_minutes),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_access_token(token: str) -> str:
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    if payload.get("type") != "access" or not payload.get("sub"):
        raise jwt.InvalidTokenError("Invalid access token")
    return str(payload["sub"])


def create_media_token(media_id: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": media_id, "type": "media", "iat": now, "exp": now + timedelta(hours=2)},
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_media_token(token: str) -> str:
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    if payload.get("type") != "media" or not payload.get("sub"):
        raise jwt.InvalidTokenError("Invalid media token")
    return str(payload["sub"])


def create_oauth_state(nonce: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "type": "google_oauth",
            "nonce": nonce,
            "iat": now,
            "exp": now + timedelta(minutes=10),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def verify_oauth_state(state: str) -> str:
    settings = get_settings()
    payload = jwt.decode(state, settings.jwt_secret, algorithms=["HS256"])
    if payload.get("type") != "google_oauth":
        raise jwt.InvalidTokenError("Invalid OAuth state")
    nonce = payload.get("nonce")
    if not nonce:
        raise jwt.InvalidTokenError("Invalid OAuth state")
    return str(nonce)


def new_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def refresh_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=get_settings().refresh_token_days)
