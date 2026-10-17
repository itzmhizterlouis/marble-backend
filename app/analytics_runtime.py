"""Shared analytics queue reservations, request pacing and provider cooldowns."""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
import uuid
from email.utils import parsedate_to_datetime

from redis import Redis

from .config import get_settings

RESERVATION_TTL = 86400
REFRESH_COOLDOWN = 900
REQUEST_INTERVAL_MS = 4000  # At most 75 live reads / 5 minutes, leaving headroom.


class AnalyticsDeferred(Exception):
    def __init__(self, seconds: float):
        self.seconds = max(1, math.ceil(seconds))
        super().__init__("Analytics refresh deferred until provider capacity is available")


def redis_client():
    return Redis.from_url(get_settings().redis_url, decode_responses=True,
                          socket_connect_timeout=3, socket_timeout=3)


def provider_key() -> str:
    digest = hashlib.sha256(get_settings().upload_post_api_key.encode()).hexdigest()[:16]
    return f"analytics:provider:{digest}"


def user_key(user_id: str) -> str:
    return f"analytics:refresh:{user_id}"


RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
RESERVE = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  if ARGV[3] == '1' then redis.call('SET', KEYS[3], '1', 'EX', ARGV[2]) end
  return 0
end
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('SET', KEYS[3], ARGV[3], 'EX', ARGV[2])
return 1
"""


def enqueue_analytics(user_id: str, force: bool = False) -> bool:
    from .tasks import run_analytics_refresh

    key, token = user_key(user_id), uuid.uuid4().hex
    with redis_client() as redis:
        if not redis.eval(RESERVE, 3, key, key + ':completed', key + ':force', token, RESERVATION_TTL, int(force)):
            return False
        try:
            run_analytics_refresh.apply_async(args=[user_id, force, token, time.time()], queue='analytics')
        except Exception:
            redis.eval(RELEASE, 1, key, token)
            raise
    return True


PACE = """
local blocked = redis.call('PTTL', KEYS[1])
if blocked > 0 then return {blocked, 1} end
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
local next = tonumber(redis.call('GET', KEYS[2]) or '0')
if next > now then return {next - now, 0} end
redis.call('SET', KEYS[2], now + ARGV[1], 'PX', ARGV[1])
return {0, 0}
"""
BLOCK = """
local ttl = redis.call('PTTL', KEYS[1])
if ttl < tonumber(ARGV[1]) then redis.call('SET', KEYS[1], '1', 'PX', ARGV[1]) end
"""


async def pace_analytics() -> None:
    key = provider_key()
    while True:
        with redis_client() as redis:
            delay, blocked = redis.eval(PACE, 2, key + ':blocked', key + ':next', REQUEST_INTERVAL_MS)
        if blocked:
            raise AnalyticsDeferred(delay / 1000)
        if not delay:
            return
        await asyncio.sleep(delay / 1000)


def retry_delay(headers, *, now: float | None = None) -> int:
    now = time.time() if now is None else now
    delays = []
    try:
        delays.append(float(headers.get('X-RateLimit-Reset', '')) - now)
    except (ValueError, TypeError):
        pass
    retry = headers.get('Retry-After', '')
    try:
        delays.append(float(retry))
    except (ValueError, TypeError):
        try:
            delays.append(parsedate_to_datetime(retry).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            pass
    return math.ceil(max([d for d in delays if math.isfinite(d) and d > 0], default=300)) + 1


def observe_analytics_limits(response) -> None:
    try:
        exhausted = int(response.headers.get('X-RateLimit-Remaining', '100')) <= 2
    except ValueError:
        exhausted = False
    if response.status_code == 429 or exhausted:
        delay = retry_delay(response.headers)
        with redis_client() as redis:
            redis.eval(BLOCK, 1, provider_key() + ':blocked', delay * 1000)
        if response.status_code == 429:
            raise AnalyticsDeferred(delay)
