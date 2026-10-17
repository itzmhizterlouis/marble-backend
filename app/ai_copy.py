"""Finish generated suggestions before validation; never rewrite manual drafts."""
from __future__ import annotations

from .ai_adjustments import adjustment_choices
from .content import (
    ContentValidationError,
    compose_caption,
    normalize_hashtags,
    validate_ai_candidate,
    x_caption_units,
)
from .gemini import GeminiError

CAPTION_FIELDS = (
    "shared_caption", "x_caption", "linkedin_caption", "tiktok_caption",
    "instagram_caption", "facebook_caption", "youtube_description",
)


def x_failure(result: dict, code: str, max_words: int | None = None) -> dict:
    error = {"code": code, "message": "Couldn't generate the X caption. Retry X or write your own."}
    if max_words:
        error["max_words"] = max_words
    return {**result, "x_caption": "", "platform_errors": {"x": error}}


def validate_finalized_candidate(candidate: dict, platforms: list[str]) -> dict:
    # A missing X suggestion never exempts other platforms or publish requests from validation.
    result = validate_ai_candidate(candidate, [p for p in platforms if p != "x"])
    if "x" in platforms and not result.get("platform_errors", {}).get("x"):
        try:
            validate_ai_candidate(result, ["x"])
        except ContentValidationError:
            result = x_failure(result, "ai_x_invalid")
    return result


async def finalize_ai_candidate(client, candidate: dict, platforms: list[str], adjustment: str | list[str] | None,
                                usage: dict, *, retry_x: bool = False) -> tuple[dict, dict]:
    result = dict(candidate)
    previous_error = result.pop("platform_errors", {}).get("x", {}) if retry_x else {}
    result.pop("platform_errors", None)
    result.pop("x_hashtags", None)
    tags = normalize_hashtags(result.get("hashtags") or [])
    limits: dict[str, dict] = {}
    shorter = "shorter" in adjustment_choices(adjustment)
    if shorter and not retry_x:
        for field in CAPTION_FIELDS:
            if isinstance(result.get(field), str) and len(result[field].split()) > 10:
                limits[field] = {"max_words": 10}
    if "x" in platforms:
        # Reserve most of X's budget for the actual caption. Other platforms keep all tags.
        x_tags = []
        for tag in tags:
            if len(x_tags) < 2 and x_caption_units(compose_caption("", [*x_tags, tag])) <= 60:
                x_tags.append(tag)
        if x_tags != tags:
            result["x_hashtags"] = x_tags
        suffix = compose_caption("", x_tags)
        x_budget = 280 - x_caption_units(suffix) - (1 if suffix else 0)
        x_text = result.get("x_caption") or ""
        if retry_x or "x_caption" in limits or not x_text.strip() or x_caption_units(x_text) > x_budget:
            limits.setdefault("x_caption", {})["max_units"] = x_budget
            if shorter or previous_error.get("max_words"):
                limits["x_caption"]["max_words"] = 10
        if retry_x:
            # Saved, grounded copy supplies meaning; no second video analysis is needed.
            result["x_caption"] = candidate.get("shared_caption") or ""
    if not limits:
        return result, usage

    originals = {field: result.get(field) or "" for field in limits}
    try:
        repaired, repair_usage = await client.repair_copy(originals, limits)
    except GeminiError as exc:
        code = "ai_shortening_timeout" if "timeout" in exc.code else "ai_shortening_failed"
        if set(limits) == {"x_caption"}:
            return x_failure(result, code, limits["x_caption"].get("max_words")), usage
        raise GeminiError("Couldn't shorten this caption. Please try again.", code=code,
                          status_code=exc.status_code) from exc
    for field, field_limits in limits.items():
        text = repaired.get(field) if isinstance(repaired, dict) else None
        if (not isinstance(text, str) or not text.strip()
                or ("max_words" in field_limits and len(text.split()) > field_limits["max_words"])
                or ("max_units" in field_limits and x_caption_units(text.strip()) > field_limits["max_units"])):
            if field == "x_caption":
                result = x_failure(result, "ai_shortening_invalid", field_limits.get("max_words"))
                continue
            raise GeminiError("Couldn't shorten this caption. Please try again.", code="ai_shortening_invalid")
        result[field] = text.strip()
    usage = dict(usage)
    for key in ("promptTokenCount", "candidatesTokenCount"):
        usage[key] = (usage.get(key) or 0) + (repair_usage.get(key) or 0)
    return result, usage
