from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_db
from .deps import require_verified_user
from .models import SocialConnection, User
from .providers import ProviderError, UploadPostClient
from .schemas import AuthorizeOut, ConnectionOut, FacebookPageOut, FacebookPageSelection, MessageOut, Platform

router = APIRouter(prefix="/v1/connections", tags=["connections"])
PLATFORMS: tuple[str, ...] = ("tiktok", "instagram", "youtube", "facebook")


def _provider_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _provider_identifier(value: object) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def connection_out(item: SocialConnection) -> ConnectionOut:
    return ConnectionOut(
        platform=item.platform,
        status=item.status,
        username=item.username,
        handle=item.handle,
        display_name=item.display_name,
        avatar_url=item.avatar_url,
        capabilities=item.capabilities or [],
        reauth_required=item.reauth_required,
        target_page_id=item.target_page_id,
        connected_at=item.connected_at,
        last_used_at=item.last_used_at,
    )


async def ensure_rows(db: AsyncSession, user: User) -> list[SocialConnection]:
    existing = {
        item.platform: item
        for item in await db.scalars(select(SocialConnection).where(SocialConnection.user_id == user.id))
    }
    for platform in PLATFORMS:
        if platform not in existing:
            existing[platform] = SocialConnection(user_id=user.id, platform=platform)
            db.add(existing[platform])
    await db.commit()
    return [existing[platform] for platform in PLATFORMS]


async def sync_connections(db: AsyncSession, user: User) -> list[SocialConnection]:
    rows = await ensure_rows(db, user)
    client = UploadPostClient()
    try:
        payload = await client.get_profile(user.upload_post_profile)
    except ProviderError as exc:
        if exc.status_code == 404:
            await client.create_profile(user.upload_post_profile)
            payload = {"profile": {"social_accounts": {}}}
        else:
            raise
    profile = payload.get("profile", payload)
    accounts = profile.get("social_accounts") or {}
    for row in rows:
        account = accounts.get(row.platform)
        now = datetime.now(UTC)
        row.last_synced_at = now
        if not account:
            row.status = "disconnected"
            row.reauth_required = False
            row.provider_account_id = None
            row.username = row.handle = row.display_name = row.avatar_url = None
            row.capabilities = []
            row.connected_at = None
            continue
        if isinstance(account, str):
            identifier = _provider_text(account)
            row.status = "connected"
            row.reauth_required = False
            row.provider_account_id = identifier
            row.username = identifier
            row.handle = None
            row.display_name = None
            row.avatar_url = None
            row.capabilities = []
        else:
            row.reauth_required = bool(account.get("reauth_required") or profile.get("reauth_required"))
            provider_status = str(account.get("status") or "").lower()
            if provider_status in {"disconnected", "expired", "failed", "unavailable", "quota_limited"}:
                row.status = provider_status
            else:
                row.status = "expired" if row.reauth_required else "connected"
            provider_username = _provider_text(account.get("username"))
            row.provider_account_id = (
                _provider_identifier(account.get("id"))
                or _provider_identifier(account.get("account_id"))
                or provider_username
            )
            row.username = provider_username
            row.handle = _provider_text(account.get("handle"))
            row.display_name = _provider_text(account.get("display_name"))
            row.avatar_url = _provider_text(account.get("social_images")) or _provider_text(account.get("avatar_url"))
            row.capabilities = account.get("capabilities") or []
        if row.status in {"connected", "needs_selection"} and not row.connected_at:
            row.connected_at = now
    facebook = next((item for item in rows if item.platform == "facebook"), None)
    if facebook and facebook.status == "connected":
        try:
            pages = await client.facebook_pages(user.upload_post_profile)
        except ProviderError:
            pages = []
        if len(pages) == 1 and not facebook.target_page_id:
            facebook.target_page_id = str(pages[0].get("page_id") or pages[0].get("id"))
        elif len(pages) > 1 and not facebook.target_page_id:
            facebook.status = "needs_selection"
    await db.commit()
    return rows


@router.get("", response_model=list[ConnectionOut])
async def list_connections(user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    rows = await ensure_rows(db, user)
    return [connection_out(item) for item in rows]


@router.post("/sync", response_model=list[ConnectionOut])
async def synchronize(user: User = Depends(require_verified_user), db: AsyncSession = Depends(get_db)):
    try:
        rows = await sync_connections(db, user)
    except ProviderError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    return [connection_out(item) for item in rows]


@router.post("/{platform}/authorize", response_model=AuthorizeOut)
async def authorize(platform: Platform, user: User = Depends(require_verified_user)):
    redirect_url = f"{get_settings().frontend_url}/accounts?connect_platform={platform}"
    client = UploadPostClient()
    try:
        try:
            await client.get_profile(user.upload_post_profile)
        except ProviderError as exc:
            if exc.status_code != 404:
                raise
            await client.create_profile(user.upload_post_profile)
        url = await client.connection_url(user.upload_post_profile, platform, redirect_url)
    except ProviderError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    return AuthorizeOut(authorize_url=url)


@router.post("/{platform}/manage", response_model=AuthorizeOut)
async def manage_connection(platform: Platform, user: User = Depends(require_verified_user)):
    """Open Upload-Post's branded manager for reconnecting or disconnecting one account."""
    redirect_url = f"{get_settings().frontend_url}/accounts?connect_platform={platform}"
    client = UploadPostClient()
    try:
        url = await client.connection_access_url(user.upload_post_profile, platform, redirect_url)
    except ProviderError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    return AuthorizeOut(authorize_url=url)


@router.get("/facebook/pages", response_model=list[FacebookPageOut])
async def facebook_pages(user: User = Depends(require_verified_user)):
    try:
        pages = await UploadPostClient().facebook_pages(user.upload_post_profile)
    except ProviderError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    return [
        FacebookPageOut(
            id=str(item.get("page_id") or item.get("id")),
            name=item.get("page_name") or item.get("name") or "Facebook Page",
        )
        for item in pages
    ]


@router.patch("/facebook", response_model=MessageOut)
async def select_facebook_page(
    payload: FacebookPageSelection,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    connection = await db.scalar(
        select(SocialConnection).where(
            SocialConnection.user_id == user.id, SocialConnection.platform == "facebook"
        )
    )
    if not connection or connection.status not in {"connected", "needs_selection"}:
        raise HTTPException(
            status_code=409, detail={"code": "facebook_not_connected", "message": "Connect Facebook first"}
        )
    pages = await UploadPostClient().facebook_pages(user.upload_post_profile)
    if payload.page_id not in {str(item.get("page_id") or item.get("id")) for item in pages}:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_facebook_page", "message": "Choose a page connected to this profile"},
        )
    connection.target_page_id = payload.page_id
    connection.status = "connected"
    await db.commit()
    return MessageOut(message="Facebook Page selected")
