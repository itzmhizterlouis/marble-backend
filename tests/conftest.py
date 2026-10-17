import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_ROOT = Path(__file__).parent / ".runtime"
TEST_ROOT.mkdir(exist_ok=True)
database_file = TEST_ROOT / "test.db"
database_file.unlink(missing_ok=True)
os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database_file}"
os.environ["STORAGE_ROOT"] = str(TEST_ROOT / "data")
os.environ["JWT_SECRET"] = "test-secret-with-at-least-thirty-two-characters"

from app.main import app  # noqa: E402


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def isolate_analytics_rate_store(monkeypatch, request):
    # Ordinary provider tests exercise HTTP payloads. Dedicated runtime tests use
    # a disposable real Redis instance to check cross-client coordination.
    if request.node.get_closest_marker("analytics_redis"):
        return
    async def no_wait():
        pass
    monkeypatch.setattr("app.analytics_runtime.pace_analytics", no_wait)
    monkeypatch.setattr("app.analytics_runtime.observe_analytics_limits", lambda response: None)
