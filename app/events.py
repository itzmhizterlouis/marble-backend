from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError
from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal
from .deps import bearer
from .models import User
from .security import decode_access_token

router = APIRouter(prefix="/v1/events", tags=["events"])
logger = logging.getLogger(__name__)


def user_channel(user_id: str) -> str:
    return f"marble:user:{user_id}:events"


async def publish_realtime_event(user_id: str, event_type: str, **identifiers: str) -> None:
    """Publish a best-effort state notification after durable state has committed."""
    settings = get_settings()
    if settings.environment.lower() == "test":
        return
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    payload = json.dumps(
        {
            "id": str(uuid.uuid4()),
            "type": event_type,
            **identifiers,
            "occurred_at": datetime.now(UTC).isoformat(),
        }
    )
    try:
        await redis.publish(user_channel(user_id), payload)
    except RedisError:
        logger.warning("Could not publish realtime event %s", event_type, exc_info=True)
    finally:
        await redis.aclose()


async def publish_post_event(user_id: str, post_id: str, event_type: str = "post.updated") -> None:
    await publish_realtime_event(user_id, event_type, post_id=post_id)


async def publish_media_event(user_id: str, media_id: str) -> None:
    await publish_realtime_event(user_id, "media.updated", media_id=media_id)


async def authenticated_user_id(credentials: HTTPAuthorizationCredentials | None) -> str:
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail={"code": "not_authenticated", "message": "Sign in to receive updates"},
        )
    try:
        user_id = decode_access_token(credentials.credentials)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_token", "message": "Your session has expired"},
        ) from exc
    async with SessionLocal() as db:
        exists = await db.scalar(select(User.id).where(User.id == user_id))
    if not exists:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_token", "message": "Your session has expired"},
        )
    return user_id


def sse_message(payload: str, event_id: str | None = None) -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.extend(("event: message", f"data: {payload}", ""))
    return "\n".join(lines) + "\n"


async def event_stream(request: Request, redis: Redis, pubsub: PubSub) -> AsyncIterator[str]:
    try:
        yield "retry: 3000\n\n"
        yield ": connected\n\n"
        while not await request.is_disconnected():
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=20)
            if message and message.get("type") == "message":
                payload = str(message["data"])
                try:
                    event_id = str(json.loads(payload).get("id") or "") or None
                except (TypeError, ValueError):
                    event_id = None
                yield sse_message(payload, event_id)
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(0)
    except (asyncio.CancelledError, RedisError):
        return
    finally:
        try:
            await pubsub.unsubscribe()
            await pubsub.aclose()
        finally:
            await redis.aclose()


@router.get("")
async def events(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    user_id = await authenticated_user_id(credentials)
    settings = get_settings()
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=30,
    )
    pubsub = redis.pubsub()
    try:
        await pubsub.subscribe(user_channel(user_id))
    except RedisError as exc:
        await pubsub.aclose()
        await redis.aclose()
        raise HTTPException(
            status_code=503,
            detail={"code": "events_unavailable", "message": "Live updates are temporarily unavailable"},
        ) from exc
    return StreamingResponse(
        event_stream(request, redis, pubsub),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
