import json

from app.config import get_settings
from app.events import publish_post_event, sse_message, user_channel


def test_event_stream_requires_authentication(client):
    response = client.get("/v1/events")
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_sse_message_contains_event_id_and_payload():
    payload = json.dumps({"id": "evt-1", "type": "post.updated", "post_id": "post-1"})
    message = sse_message(payload, "evt-1")
    assert message == f"id: evt-1\nevent: message\ndata: {payload}\n\n"


async def test_post_event_is_published_to_the_users_channel(monkeypatch):
    delivered = {}

    class FakeRedis:
        async def publish(self, channel, payload):
            delivered["channel"] = channel
            delivered["payload"] = json.loads(payload)

        async def aclose(self):
            delivered["closed"] = True

    monkeypatch.setattr(get_settings(), "environment", "development")
    monkeypatch.setattr("app.events.Redis.from_url", lambda *_args, **_kwargs: FakeRedis())
    await publish_post_event("user-1", "post-1")

    assert delivered["channel"] == user_channel("user-1")
    assert delivered["payload"]["type"] == "post.updated"
    assert delivered["payload"]["post_id"] == "post-1"
    assert delivered["closed"] is True
