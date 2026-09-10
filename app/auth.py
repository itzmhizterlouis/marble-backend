from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db
from .deps import get_current_user
from .email import send_password_reset_email, send_verification_email
from .models import AuthSession, OneTimeToken, User
from .schemas import AuthResponse, EmailIn, LoginIn, MessageOut, PasswordResetIn, RegisterIn, TokenIn, UserOut
from .security import (
    create_access_token,
    create_oauth_state,
    hash_password,
    new_opaque_token,
    refresh_expiry,
    token_hash,
    verify_oauth_state,
    verify_password,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])
REFRESH_COOKIE = "marble_refresh"
GOOGLE_STATE_COOKIE = "marble_google_state"


def normalize_email(email: str) -> str:
    return email.strip().lower()


def expired(value: datetime) -> bool:
    comparable = value if value.tzinfo else value.replace(tzinfo=UTC)
    return comparable <= datetime.now(UTC)


def user_out(user: User) -> UserOut:
    return UserOut.model_validate(user)


def set_refresh_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_days * 86400,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )


async def create_session(db: AsyncSession, user: User, request: Request) -> tuple[str, str]:
    refresh = new_opaque_token()
    session = AuthSession(
        user_id=user.id,
        refresh_hash=token_hash(refresh),
        expires_at=refresh_expiry(),
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    db.add(session)
    await db.commit()
    return create_access_token(user.id), refresh


async def create_one_time_token(db: AsyncSession, user: User, purpose: str, minutes: int) -> str:
    raw = new_opaque_token()
    db.add(
        OneTimeToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=token_hash(raw),
            expires_at=datetime.now(UTC) + timedelta(minutes=minutes),
        )
    )
    await db.flush()
    return raw


async def consume_one_time_token(db: AsyncSession, raw: str, purpose: str) -> User:
    item = await db.scalar(
        select(OneTimeToken).where(
            OneTimeToken.token_hash == token_hash(raw),
            OneTimeToken.purpose == purpose,
            OneTimeToken.used_at.is_(None),
            OneTimeToken.expires_at > datetime.now(UTC),
        )
    )
    if not item:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_or_expired_token", "message": "This link is invalid or expired"},
        )
    user = await db.get(User, item.user_id)
    if not user:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_or_expired_token", "message": "This link is invalid or expired"},
        )
    item.used_at = datetime.now(UTC)
    return user


@router.post("/register", response_model=AuthResponse, status_code=201)
async def register(
    payload: RegisterIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    email = normalize_email(payload.email)
    if await db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(
            status_code=409, detail={"code": "email_taken", "message": "An account already uses this email"}
        )
    user = User(
        email=email,
        name=payload.name.strip(),
        password_hash=hash_password(payload.password),
        upload_post_profile=f"marble_{new_opaque_token()[:20].lower()}",
    )
    db.add(user)
    await db.flush()
    verify_token = await create_one_time_token(db, user, "verify_email", 24 * 60)
    await send_verification_email(user.email, user.name, verify_token)
    access, refresh = await create_session(db, user, request)
    set_refresh_cookie(response, refresh)
    return AuthResponse(access_token=access, user=user_out(user))


@router.post("/login", response_model=AuthResponse)
async def login(payload: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == normalize_email(payload.email)))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_credentials", "message": "Email or password is incorrect"},
        )
    access, refresh = await create_session(db, user, request)
    set_refresh_cookie(response, refresh)
    return AuthResponse(access_token=access, user=user_out(user))


@router.post("/refresh", response_model=AuthResponse)
async def refresh_session(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    if not refresh_token:
        raise HTTPException(
            status_code=401, detail={"code": "missing_refresh_token", "message": "Sign in to continue"}
        )
    item = await db.scalar(
        select(AuthSession)
        .where(AuthSession.refresh_hash == token_hash(refresh_token))
        .with_for_update()
    )
    if not item or item.revoked_at or expired(item.expires_at):
        raise HTTPException(
            status_code=401, detail={"code": "invalid_refresh_token", "message": "Sign in again"}
        )
    user = await db.get(User, item.user_id)
    item.revoked_at = datetime.now(UTC)
    access, rotated = await create_session(db, user, request)
    set_refresh_cookie(response, rotated)
    return AuthResponse(access_token=access, user=user_out(user))


@router.post("/logout", response_model=MessageOut)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    if refresh_token:
        item = await db.scalar(
            select(AuthSession).where(AuthSession.refresh_hash == token_hash(refresh_token))
        )
        if item:
            item.revoked_at = datetime.now(UTC)
            await db.commit()
    response.delete_cookie(REFRESH_COOKIE, path="/")
    return MessageOut(message="Signed out")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user_out(user)


@router.post("/verify", response_model=UserOut)
async def verify_email(payload: TokenIn, db: AsyncSession = Depends(get_db)):
    user = await consume_one_time_token(db, payload.token, "verify_email")
    user.email_verified = True
    await db.commit()
    return user_out(user)


@router.post("/resend-verification", response_model=MessageOut)
async def resend_verification(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if user.email_verified:
        return MessageOut(message="Email is already verified")
    token = await create_one_time_token(db, user, "verify_email", 24 * 60)
    await send_verification_email(user.email, user.name, token)
    await db.commit()
    return MessageOut(message="Verification email sent")


@router.post("/forgot-password", response_model=MessageOut)
async def forgot_password(payload: EmailIn, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == normalize_email(payload.email)))
    if user:
        token = await create_one_time_token(db, user, "reset_password", 60)
        await send_password_reset_email(user.email, user.name, token)
        await db.commit()
    return MessageOut(message="If that account exists, a reset link has been sent")


@router.post("/reset-password", response_model=MessageOut)
async def reset_password(payload: PasswordResetIn, db: AsyncSession = Depends(get_db)):
    user = await consume_one_time_token(db, payload.token, "reset_password")
    user.password_hash = hash_password(payload.password)
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await db.commit()
    return MessageOut(message="Password updated. Sign in with your new password")


@router.get("/google/start")
async def google_start(response: Response):
    authorize_url = prepare_google_authorization(response)
    return {"authorize_url": authorize_url}


def prepare_google_authorization(response: Response) -> str:
    settings = get_settings()
    if not settings.google_client_id:
        raise HTTPException(
            status_code=503,
            detail={"code": "google_not_configured", "message": "Google sign-in is not configured"},
        )
    nonce = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "select_account",
            "state": create_oauth_state(nonce),
        }
    )
    response.set_cookie(
        GOOGLE_STATE_COOKIE,
        nonce,
        max_age=600,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/v1/auth/google/callback",
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


@router.get("/google/authorize")
async def google_authorize():
    """Start OAuth as a top-level backend navigation so its state cookie stays first-party."""
    response = RedirectResponse(url="https://accounts.google.com", status_code=302)
    response.headers["location"] = prepare_google_authorization(response)
    return response


@router.get("/google/callback")
async def google_callback(
    code: str,
    state: str,
    state_cookie: str | None = Cookie(default=None, alias=GOOGLE_STATE_COOKIE),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    try:
        state_nonce = verify_oauth_state(state)
        if not state_cookie or not secrets.compare_digest(state_nonce, state_cookie):
            raise jwt.InvalidTokenError("OAuth state does not match this browser")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_oauth_state", "message": "Google sign-in expired"}
        ) from exc
    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]
        profile_response = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        profile_response.raise_for_status()
    profile = profile_response.json()
    email = normalize_email(profile["email"])
    user = await db.scalar(select(User).where((User.google_sub == profile["sub"]) | (User.email == email)))
    if not user:
        user = User(
            email=email,
            name=profile.get("name") or email.split("@")[0],
            google_sub=profile["sub"],
            email_verified=bool(profile.get("email_verified", True)),
            upload_post_profile=f"marble_{new_opaque_token()[:20].lower()}",
        )
        db.add(user)
    else:
        user.google_sub = profile["sub"]
        user.email_verified = user.email_verified or bool(profile.get("email_verified"))
    await db.commit()
    exchange = await create_one_time_token(db, user, "oauth_exchange", 5)
    await db.commit()
    redirect = RedirectResponse(f"{settings.frontend_url}/auth/callback?code={exchange}", status_code=302)
    redirect.delete_cookie(GOOGLE_STATE_COOKIE, path="/v1/auth/google/callback")
    return redirect


@router.post("/exchange", response_model=AuthResponse)
async def exchange_code(
    payload: TokenIn, request: Request, response: Response, db: AsyncSession = Depends(get_db)
):
    user = await consume_one_time_token(db, payload.token, "oauth_exchange")
    access, refresh = await create_session(db, user, request)
    set_refresh_cookie(response, refresh)
    await db.commit()
    return AuthResponse(access_token=access, user=user_out(user))
