import asyncio
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import respx
from celery.exceptions import Retry
from redis import Redis

from app import analytics_runtime as runtime
from app.config import get_settings
from app.providers import UploadPostClient
from app.tasks import run_analytics_refresh

pytestmark = pytest.mark.analytics_redis


@pytest.fixture(scope="module")
def redis_url(tmp_path_factory):
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server required for cross-worker coordination tests")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    directory = tmp_path_factory.mktemp("analytics-redis")
    process = subprocess.Popen([binary, "--bind", "127.0.0.1", "--port", str(port),
                                "--save", "", "--appendonly", "no", "--dir", str(directory)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"redis://127.0.0.1:{port}/0"
    client = Redis.from_url(url)
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except ConnectionError:
                pass
            except Exception:
                time.sleep(.02)
        else:
            raise RuntimeError("Test Redis did not start")
        yield url
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)


@pytest.fixture(autouse=True)
def store(redis_url, monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_url", redis_url)
    monkeypatch.setattr(get_settings(), "upload_post_api_key", "test-analytics-key")
    monkeypatch.setattr(get_settings(), "upload_post_base_url", "https://provider.test/api")
    with runtime.redis_client() as redis:
        redis.flushdb()  # Only the isolated server created above.
        yield redis


def test_simultaneous_refreshes_queue_once_and_release_on_broker_failure(monkeypatch, store):
    calls = []
    monkeypatch.setattr(run_analytics_refresh, "apply_async", lambda **kwargs: calls.append(kwargs))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: runtime.enqueue_analytics("creator", True), range(16)))
    assert results.count(True) == 1
    assert len(calls) == 1 and calls[0]["queue"] == "analytics"
    def fail(**kwargs):
        raise RuntimeError("Broker unavailable")
    monkeypatch.setattr(run_analytics_refresh, "apply_async", fail)
    with pytest.raises(RuntimeError):
        runtime.enqueue_analytics("other", True)
    assert store.get(runtime.user_key("other")) is None


async def test_pacing_is_shared_between_independent_clients(monkeypatch, store):
    assert runtime.REQUEST_INTERVAL_MS == 4000
    monkeypatch.setattr(runtime, "REQUEST_INTERVAL_MS", 40)
    times = []
    async def request():
        await runtime.pace_analytics()
        times.append(time.monotonic())
    await asyncio.gather(*(request() for _ in range(5)))
    assert all(b - a >= .032 for a, b in zip(times, times[1:], strict=False))


@respx.mock
async def test_429_pauses_other_profiles_until_reset_without_another_http_call(store):
    route = respx.get("https://provider.test/api/uploadposts/post-analytics/post").mock(
        return_value=httpx.Response(429, headers={"X-RateLimit-Reset": str(time.time() + 90), "Retry-After": "120"},
                                   json={"message": "Too many requests"}))
    with pytest.raises(runtime.AnalyticsDeferred) as first:
        await UploadPostClient().post_analytics(request_id="post", profile="first")
    assert first.value.seconds >= 120
    with pytest.raises(runtime.AnalyticsDeferred):
        await UploadPostClient().post_analytics(request_id="post", profile="second")
    assert route.call_count == 1
    assert store.ttl(runtime.provider_key() + ':blocked') >= 119


def test_reset_headers_and_exhausted_general_budget(store):
    assert runtime.retry_delay({"Retry-After": "Thu, 01 Jan 1970 00:02:00 GMT"}, now=60) == 61
    assert runtime.retry_delay({"Retry-After": "broken", "X-RateLimit-Reset": "nan"}, now=60) == 301
    runtime.observe_analytics_limits(httpx.Response(200, headers={"X-RateLimit-Remaining": "0", "Retry-After": "20"}))
    with pytest.raises(runtime.AnalyticsDeferred):
        asyncio.run(runtime.pace_analytics())


def test_deferred_worker_keeps_reservation_then_completes_once(monkeypatch, store):
    key, token = runtime.user_key("creator"), "owner"
    store.set(key, token, ex=runtime.RESERVATION_TTL)
    attempts, retries = [], []
    async def work(user_id, force, started_at):
        attempts.append(started_at)
        if len(attempts) == 1:
            raise runtime.AnalyticsDeferred(91)
    def retry(**kwargs):
        retries.append(kwargs)
        return Retry()
    monkeypatch.setattr("app.tasks._sync_analytics", work)
    monkeypatch.setattr(run_analytics_refresh, "retry", retry)
    with pytest.raises(Retry):
        run_analytics_refresh.run("creator", True, token, 123)
    assert retries[0]["countdown"] == 91
    assert store.get(key) == token and not store.exists(key + ':running')
    run_analytics_refresh.run("creator", True, token, 123)
    assert attempts == [123, 123]
    assert not store.exists(key)
    assert store.ttl(key + ':completed') > 890
    assert runtime.enqueue_analytics("creator", True) is False
    run_analytics_refresh.run("creator", True, token, 123)
    assert len(attempts) == 2


def test_duplicate_delivery_does_not_release_another_workers_lock(monkeypatch, store):
    key = runtime.user_key("creator")
    store.set(key, "owner", ex=86400)
    store.set(key + ':running', "active-worker", ex=120)
    monkeypatch.setattr(run_analytics_refresh, "retry", lambda **kwargs: Retry())
    with pytest.raises(Retry):
        run_analytics_refresh.run("creator", True, "owner", 123)
    assert store.get(key) == "owner"
    assert store.get(key + ':running') == "active-worker"


def test_manual_refresh_clicks_coalesce_before_worker_starts(client, monkeypatch):
    from test_post_performance import creator

    _, headers = creator(client, monkeypatch)
    calls = []
    monkeypatch.setattr(run_analytics_refresh, "apply_async", lambda **kwargs: calls.append(kwargs))
    first = client.post("/v1/analytics/refresh", headers=headers)
    second = client.post("/v1/analytics/refresh", headers=headers)
    assert first.status_code == second.status_code == 202
    assert len(calls) == 1


def test_manual_request_upgrades_queued_automatic_refresh(monkeypatch, store):
    calls = []
    monkeypatch.setattr(run_analytics_refresh, "apply_async", lambda **kwargs: calls.append(kwargs))
    assert runtime.enqueue_analytics("creator", False)
    assert runtime.enqueue_analytics("creator", True) is False
    force_values = []
    async def work(user_id, force, started_at):
        force_values.append(force)
    monkeypatch.setattr("app.tasks._sync_analytics", work)
    run_analytics_refresh.run(*calls[0]["args"])
    assert force_values == [True]
