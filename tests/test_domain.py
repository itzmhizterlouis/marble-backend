from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.media import append_chunk, parse_content_range
from app.posts import validate_schedule
from app.tasks import provider_result_status


def test_content_range_and_resumable_offsets(tmp_path):
    assert parse_content_range("bytes 0-3/8") == (0, 3, 8)
    target = tmp_path / "video.mp4"
    assert append_chunk(target, b"abcd", 0, 3, 8, 0) == 4
    assert append_chunk(target, b"efgh", 4, 7, 8, 4) == 8
    assert target.read_bytes() == b"abcdefgh"
    with pytest.raises(ValueError, match="Expected byte offset"):
        append_chunk(target, b"x", 3, 3, 8, 8)


def test_schedule_boundaries_and_timezone():
    valid = datetime.now(UTC) + timedelta(minutes=15)
    assert validate_schedule(valid, "Africa/Lagos").tzinfo == UTC
    with pytest.raises(HTTPException) as too_soon:
        validate_schedule(datetime.now(UTC) + timedelta(minutes=5), "Africa/Lagos")
    assert too_soon.value.detail["code"] == "schedule_too_soon"
    with pytest.raises(HTTPException) as too_far:
        validate_schedule(datetime.now(UTC) + timedelta(days=366), "Africa/Lagos")
    assert too_far.value.detail["code"] == "schedule_too_far"


def test_provider_status_mapping():
    assert provider_result_status({"status": "processing"}) == "publishing"
    assert provider_result_status({"status": "retryable", "success": False}) == "publishing"
    assert provider_result_status({"success": True}) == "published"
    assert provider_result_status({"success": False, "error": "expired"}) == "failed"
    assert provider_result_status({"fallback_to_inbox": True}) == "action_required"
