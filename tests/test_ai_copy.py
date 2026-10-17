from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.ai_copy import CAPTION_FIELDS, finalize_ai_candidate, validate_finalized_candidate
from app.content import (
    ContentValidationError,
    compile_and_validate,
    compose_caption,
    validate_ai_candidate,
    x_caption_units,
)
from app.gemini import GeminiError


def candidate():
    return {**dict.fromkeys(CAPTION_FIELDS, "See the blue stitching on this handmade bag."),
            "hashtags": ["Handmade", "Bags"], "youtube_title": "Handmade bag", "facebook_title": "Blue stitching"}


@pytest.mark.asyncio
async def test_valid_suggestions_need_no_extra_request():
    client = AsyncMock()
    original = candidate()
    result, usage = await finalize_ai_candidate(client, original, ["x", "instagram"], None, {"promptTokenCount": 5})
    assert result == original
    assert usage == {"promptTokenCount": 5}
    client.repair_copy.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_repair", [False, True])
async def test_shorter_is_enforced_even_if_model_ignores_limit_or_repair_fails(failed_repair):
    original = candidate()
    long = "Here is a handmade bag with beautiful blue stitching and a sturdy handle for carrying your belongings."
    for field in CAPTION_FIELDS:
        original[field] = long
    client = AsyncMock()
    client.repair_copy.return_value = (original, {})
    if failed_repair:
        client.repair_copy.side_effect = GeminiError("rate limited")
    with pytest.raises(GeminiError) as error:
        await finalize_ai_candidate(client, original, ["x", "instagram", "youtube"], ["shorter", "more_engaging"], {})
    assert str(error.value) == "Couldn't shorten this caption. Please try again."
    assert error.value.code == ("ai_shortening_failed" if failed_repair else "ai_shortening_invalid")
    assert original["shared_caption"] == long
    client.repair_copy.assert_awaited_once()


@pytest.mark.asyncio
async def test_x_repair_changes_only_x_and_accounts_for_hashtags_and_tokens():
    original = candidate()
    original["x_caption"] = "A handmade bag with blue stitching. " * 8
    before = deepcopy(original)
    client = AsyncMock()
    client.repair_copy.return_value = ({"x_caption": "Handmade, with blue stitching.", "instagram_caption": "Ignore this"},
                                     {"promptTokenCount": 12, "candidatesTokenCount": 5})
    result, usage = await finalize_ai_candidate(client, original, ["x", "instagram"], None,
                                              {"promptTokenCount": 20, "candidatesTokenCount": 10})
    assert original == before
    assert {k: v for k, v in result.items() if k != "x_caption"} == {k: v for k, v in original.items() if k != "x_caption"}
    assert usage == {"promptTokenCount": 32, "candidatesTokenCount": 15}
    captions, limits = client.repair_copy.call_args.args
    assert list(captions) == ["x_caption"]
    assert limits["x_caption"]["max_units"] == 264
    validate_ai_candidate(result, ["x", "instagram"])


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["👨‍👩‍👧‍👦 " * 160, "東" * 150, "https://example.com/" + "a" * 350])
async def test_x_uses_weighted_length_and_never_returns_a_cut_off_fallback(body):
    original = {**candidate(), "x_caption": body}
    client = AsyncMock()
    client.repair_copy.side_effect = GeminiError("rate limited")
    if body.startswith("https:"):
        result, _ = await finalize_ai_candidate(client, original, ["x"], None, {})
        assert result == original
        assert x_caption_units(compose_caption(result["x_caption"], result["hashtags"])) <= 280
        client.repair_copy.assert_not_called()
    else:
        result, _ = await finalize_ai_candidate(client, original, ["x"], None, {})
        assert result["x_caption"] == ""
        assert result["platform_errors"]["x"]["code"] == "ai_shortening_failed"
        assert original["x_caption"] == body


@pytest.mark.asyncio
async def test_x_can_use_fewer_tags_without_altering_other_platforms():
    original = {**candidate(), "hashtags": [f"LongTag{i}" + "x" * 40 for i in range(10)]}
    client = AsyncMock()
    client.repair_copy.return_value = ({"x_caption": "Blue stitching, handmade."}, {})
    result, _ = await finalize_ai_candidate(client, original, ["x", "instagram"], None, {})
    assert result["hashtags"] == original["hashtags"]
    assert result["instagram_caption"] == original["instagram_caption"]
    assert len(result["x_hashtags"]) < len(original["hashtags"])
    validate_ai_candidate(result, ["x", "instagram"])
    options = {"ai_hashtags": result["x_hashtags"], "ai_hashtags_source": result["hashtags"]}
    versions = [{"platform": "x", "caption": result["x_caption"], "use_shared_caption": False, "options": options}]
    compiled = compile_and_validate(shared_caption="Shared", hashtags=result["hashtags"], versions=versions, content_format_version=2)
    assert compiled[0].caption == compose_caption(result["x_caption"], result["x_hashtags"])
    assert compiled[0].hashtags == result["x_hashtags"]
    # New creator-supplied tags supersede the AI selection and are validated normally.
    with pytest.raises(ContentValidationError):
        compile_and_validate(shared_caption="Shared", hashtags=[*result["hashtags"], "New"], versions=versions, content_format_version=2)


@pytest.mark.asyncio
async def test_x_repair_does_not_bypass_other_platform_validation():
    original = {**candidate(), "x_caption": "long " * 80, "youtube_title": "t" * 101}
    client = AsyncMock()
    client.repair_copy.return_value = ({"x_caption": "Short copy."}, {})
    result, _ = await finalize_ai_candidate(client, original, ["x", "youtube"], None, {})
    with pytest.raises(ContentValidationError) as error:
        validate_ai_candidate(result, ["x", "youtube"])
    assert "versions.youtube.title" in error.value.field_errors


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "invalid"])
async def test_x_failure_preserves_other_captions_and_retry_changes_only_x(failure):
    original = {**candidate(), "x_caption": "long " * 100}
    client = AsyncMock()
    if failure == "timeout":
        client.repair_copy.side_effect = GeminiError("Timed out", code="gemini_generation_timeout")
    else:
        client.repair_copy.return_value = ({"x_caption": "Still long " * 60}, {})
    partial, _ = await finalize_ai_candidate(client, original, ["x", "instagram", "youtube"], None, {})
    assert partial["x_caption"] == ""
    assert partial["instagram_caption"] == original["instagram_caption"]
    assert partial["youtube_description"] == original["youtube_description"]
    assert partial["platform_errors"]["x"]["code"] == f"ai_shortening_{failure}"
    validate_finalized_candidate(partial, ["x", "instagram", "youtube"])
    client.repair_copy.side_effect = None
    client.repair_copy.return_value = ({"x_caption": "Blue stitching, handmade.", "instagram_caption": "Must ignore"}, {})
    repaired, _ = await finalize_ai_candidate(client, partial, ["x", "instagram", "youtube"], "retry_x", {}, retry_x=True)
    assert "platform_errors" not in repaired
    assert repaired["x_caption"] == "Blue stitching, handmade."
    assert repaired["instagram_caption"] == original["instagram_caption"]
    assert list(client.repair_copy.call_args.args[0]) == ["x_caption"]
    validate_ai_candidate(repaired, ["x", "instagram", "youtube"])


@pytest.mark.asyncio
async def test_shorter_x_retry_keeps_ten_word_limit_and_survives_another_failure():
    original = {**candidate(), "x_caption": "word " * 12}
    client = AsyncMock()
    client.repair_copy.return_value = ({"x_caption": "word " * 12}, {})
    partial, _ = await finalize_ai_candidate(client, original, ["x", "instagram"], "shorter", {})
    assert partial["platform_errors"]["x"]["max_words"] == 10
    retried, _ = await finalize_ai_candidate(client, partial, ["x", "instagram"], "retry_x", {}, retry_x=True)
    assert retried == partial
    assert client.repair_copy.call_args.args[1]["x_caption"]["max_words"] == 10


def test_partial_suggestion_cannot_bypass_other_validation_or_publish_limits():
    partial = {**candidate(), "x_caption": "", "platform_errors": {"x": {"code": "timeout"}}}
    with pytest.raises(ContentValidationError):
        validate_finalized_candidate({**partial, "youtube_title": "t" * 101}, ["x", "youtube"])
    with pytest.raises(ContentValidationError):
        compile_and_validate(shared_caption="", hashtags=[], content_format_version=2,
                             versions=[{"platform": "x", "caption": "long " * 100, "use_shared_caption": False}])


@pytest.mark.asyncio
async def test_small_x_tag_budget_avoids_unnecessary_correction():
    original = {**candidate(), "x_caption": "A" * 160, "hashtags": ["handmade", "blue", "studio", "craft", "bags"]}
    client = AsyncMock()
    result, _ = await finalize_ai_candidate(client, original, ["x", "instagram"], None, {})
    assert result["x_hashtags"] == ["handmade", "blue"]
    assert result["hashtags"] == original["hashtags"]
    client.repair_copy.assert_not_called()
    validate_ai_candidate(result, ["x", "instagram"])
