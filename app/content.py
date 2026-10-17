from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from twitter_text import parse_tweet

INSTAGRAM_PROHIBITED_HASHTAGS = {
    "a$$",
    "abdl",
    "addmysc",
    "adulting",
    "alone",
    "always",
    "anorexia",
    "antivax",
    "armparty",
    "asiagirl",
    "beautyblogger",
    "besties",
    "bikinibod",
    "bikinibody",
    "blogladrona",
    "boho",
    "brain",
    "cancer",
    "costumes",
    "curvygirls",
    "date",
    "dating",
    "desk",
    "dm",
    "edm",
    "elevator",
    "endme",
    "followtrain",
    "followtrains",
    "girlsonly",
    "gloves",
    "graffitiigers",
    "happythanksgiving",
    "hardworkpaysoff",
    "hotgirls",
    "humpday",
    "hustler",
    "ifb",
    "iphonegraphy",
    "italiano",
    "kansas",
    "kill",
    "killingit",
    "killme",
    "killyourself",
    "kissing",
    "kys",
    "master",
    "midget",
    "milf",
    "models",
    "mustfollow",
    "nasty",
    "newyearsday",
    "payme",
    "petite",
    "petitegirls",
    "pushups",
    "saltwater",
    "shit",
    "shower",
    "single",
    "singlelife",
    "skype",
    "snap",
    "snapchat",
    "snapchatme",
    "snowstorm",
    "sopretty",
    "stranger",
    "streetphoto",
    "suicide",
    "suicideawareness",
    "sunbathing",
    "swole",
    "tag4like",
    "tanlines",
    "teen",
    "teens",
    "thought",
    "todayimwearing",
    "unbalanced",
    "undies",
    "valentinesday",
    "workflow",
    "yolo",
    "youngmodel",
}

MAX_HASHTAGS = 100
MAX_HASHTAG_LENGTH = 100
# Input-list safety bounds above are not platform publishing limits.
# Verified 2026-10-05: https://support.google.com/youtube/answer/6390658
PLATFORM_HASHTAG_LIMITS = {"youtube": 60}
_HASHTAG_TOKEN = re.compile(r"(?<![\w#])#([^\s#]+)", re.UNICODE)
_TRAILING_PUNCTUATION = ".,!?;:()[]{}\"'"


class ContentValidationError(ValueError):
    def __init__(self, field_errors: dict[str, str]):
        super().__init__("Check the highlighted content fields")
        self.field_errors = field_errors


@dataclass(frozen=True)
class CompiledPlatformContent:
    platform: str
    caption: str
    title: str | None
    hashtags: list[str]


def _value(item: Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def normalize_hashtags(values: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    errors: dict[str, str] = {}
    raw_values = list(values)
    if len(raw_values) > MAX_HASHTAGS:
        errors["hashtags"] = f"Use no more than {MAX_HASHTAGS} hashtags"
    for index, raw in enumerate(raw_values[:MAX_HASHTAGS]):
        tag = str(raw).strip().lstrip("#").strip()
        field = f"hashtags.{index}"
        if not tag:
            errors[field] = "Enter a hashtag"
            continue
        if any(character.isspace() for character in tag):
            errors[field] = "A hashtag cannot contain spaces"
            continue
        if len(tag) > MAX_HASHTAG_LENGTH:
            errors[field] = f"Use no more than {MAX_HASHTAG_LENGTH} characters"
            continue
        if not all(character.isalnum() or character == "_" for character in tag):
            errors[field] = "Use letters, numbers or underscores"
            continue
        key = tag.casefold()
        if key not in seen:
            normalized.append(tag)
            seen.add(key)
    if errors:
        raise ContentValidationError(errors)
    return normalized


def compose_caption(body: str, hashtags: Iterable[str]) -> str:
    caption = body.strip()
    suffix = " ".join(f"#{tag}" for tag in hashtags)
    return " ".join(part for part in (caption, suffix) if part).strip()


def utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def x_caption_units(text: str) -> int:
    """Use twitter-text weighting for emoji, URLs and normalized Unicode."""
    return parse_tweet(text).weightedLength


def validate_video_platforms(media: Any, platforms: Iterable[str]) -> None:
    """Check metadata available after Reverb's existing video processing."""
    errors = {}
    for platform in platforms:
        if platform not in {"x", "linkedin"}:
            continue
        label = "X" if platform == "x" else "LinkedIn"
        duration = media.duration_seconds
        width, height = media.width, media.height
        message = None
        if platform == "linkedin":
            if not 75_000 <= media.size_bytes <= 500_000_000:
                message = "LinkedIn videos must be between 75 KB and 500 MB."
            elif duration is not None and not 3 <= duration <= 1800:
                message = "LinkedIn videos must be between 3 seconds and 30 minutes."
            elif width and height and (not 256 <= width <= 4096 or not 144 <= height <= 2304 or not 1 / 2.4 <= width / height <= 2.4):
                message = "LinkedIn requires 256×144 to 4096×2304 pixels and an aspect ratio between 1:2.4 and 2.4:1."
        else:
            if media.size_bytes > 8_000_000_000 or (duration is not None and duration > 1200):
                message = "X videos must be at most 20 minutes and 8 GB in Reverb."
            # Upload-Post automatically adapts video geometry for each destination.
            # Let its /upload pipeline handle X dimensions/aspect ratio for now,
            # schedules and retries; keep hard duration and file-size limits.
            # https://docs.upload-post.com/api/video-requirements/#automatic-video-transformation
        if message:
            errors[f"versions.{platform}.media"] = f"{label}: {message}"
    if errors:
        raise ContentValidationError(errors)


def count_hashtags(text: str) -> int:
    """Count occurrences, not unique tags, without rewriting creator text.

    Match the frontend's Unicode letters/numbers/marks boundary rules. Ignore
    bare hashes, markdown headings, word-internal hashes and URL fragments.
    """
    count = 0
    for match in re.finditer("#", text):
        index = match.start()
        if index:
            previous = text[index - 1]
            if previous in "_/#" or unicodedata.category(previous)[0] in "LNM":
                continue
        if index + 1 < len(text):
            following = text[index + 1]
            if following == "_" or unicodedata.category(following)[0] in "LN":
                count += 1
    return count


def validate_hashtag_count(content: CompiledPlatformContent) -> None:
    limit = PLATFORM_HASHTAG_LIMITS.get(content.platform)
    if limit is None:
        return
    count = count_hashtags(content.caption) + count_hashtags(content.title or "")
    if count > limit:
        raise ContentValidationError({
            f"versions.{content.platform}.caption":
                f"You have {count} hashtags across your title and description. "
                f"Remove {count - limit}; YouTube ignores all hashtags above {limit}."
        })


def _hashtags_in_caption(caption: str) -> set[str]:
    return {
        item.strip(_TRAILING_PUNCTUATION).casefold()
        for item in _HASHTAG_TOKEN.findall(caption)
        if item.strip(_TRAILING_PUNCTUATION)
    }


def compile_platform_contents(
    *,
    shared_caption: str,
    hashtags: Iterable[str],
    versions: Iterable[Any],
    content_format_version: int,
) -> list[CompiledPlatformContent]:
    normalized_hashtags = (
        normalize_hashtags(hashtags)
        if content_format_version >= 2
        else [str(tag).strip().lstrip("#") for tag in hashtags if str(tag).strip().lstrip("#")]
    )
    compiled: list[CompiledPlatformContent] = []
    for version in versions:
        platform = str(_value(version, "platform"))
        caption = str(_value(version, "caption", "") or "")
        title_value = _value(version, "title")
        title = str(title_value).strip() if title_value else None
        options = _value(version, "options", {}) or {}
        platform_hashtags = normalized_hashtags
        # AI may propose fewer tags for X. An explicit edit to the shared tags
        # makes this proposal obsolete; manual content is never shortened here.
        if (platform == "x" and options.get("ai_hashtags_source") == normalized_hashtags
                and isinstance(options.get("ai_hashtags"), list)):
            platform_hashtags = normalize_hashtags(options["ai_hashtags"])
        if content_format_version >= 2:
            use_shared = _value(version, "use_shared_caption")
            separate = (
                not use_shared
                if use_shared is not None
                else options.get("separate_caption") is True
            )
            body = caption if separate else shared_caption
            caption = compose_caption(body, platform_hashtags)
        else:
            caption = caption.strip()
        compiled.append(
            CompiledPlatformContent(
                platform=platform,
                caption=caption,
                title=title,
                hashtags=platform_hashtags,
            )
        )
    return compiled


def validate_compiled_contents(contents: Iterable[CompiledPlatformContent]) -> None:
    contents = list(contents)
    field_errors: dict[str, str] = {}
    selected = {content.platform for content in contents}
    if {"facebook", "youtube"}.issubset(selected):
        facebook = next(content for content in contents if content.platform == "facebook")
        if not facebook.title:
            field_errors["versions.facebook.title"] = (
                "Add a Facebook title when publishing with YouTube"
            )
    for content in contents:
        platform = content.platform
        caption_field = f"versions.{platform}.caption"
        title_field = f"versions.{platform}.title"
        if not content.caption:
            field_errors[caption_field] = "Add a caption"
            continue
        if platform == "tiktok":
            count = utf16_units(content.caption)
            if count > 2200:
                field_errors[caption_field] = f"Reduce by {count - 2200} UTF-16 units"
        elif platform == "instagram":
            count = len(content.caption)
            if count > 2200:
                field_errors[caption_field] = f"Reduce by {count - 2200} characters"
            prohibited = sorted(_hashtags_in_caption(content.caption) & INSTAGRAM_PROHIBITED_HASHTAGS)
            if prohibited:
                field_errors[caption_field] = (
                    "Remove prohibited Instagram hashtag"
                    + ("s" if len(prohibited) > 1 else "")
                    + f": {', '.join(f'#{tag}' for tag in prohibited)}"
                )
        elif platform == "youtube":
            character_count = len(content.caption)
            byte_count = utf8_bytes(content.caption)
            if character_count > 5000:
                field_errors[caption_field] = f"Reduce by {character_count - 5000} characters"
            elif byte_count > 5000:
                field_errors[caption_field] = f"Reduce by {byte_count - 5000} UTF-8 bytes"
            if not content.title:
                field_errors[title_field] = "Add a YouTube title"
            elif len(content.title) > 100:
                field_errors[title_field] = f"Reduce by {len(content.title) - 100} characters"
        elif platform == "x":
            count = x_caption_units(content.caption)
            if count > 280:
                field_errors[caption_field] = "Shorten the X caption to 280 weighted characters, including hashtags. Reverb publishes one video post, not a thread."
        elif platform == "linkedin":
            count = utf16_units(content.caption)
            if count > 3000:
                field_errors[caption_field] = f"Reduce by {count - 3000} UTF-16 units"
        elif platform == "facebook":
            count = len(content.caption)
            if count > 63206:
                field_errors[caption_field] = f"Reduce by {count - 63206} characters"
            if content.title and len(content.title) > 255:
                field_errors[title_field] = f"Reduce by {len(content.title) - 255} characters"
        try:
            validate_hashtag_count(content)
        except ContentValidationError as exc:
            for field, message in exc.field_errors.items():
                field_errors.setdefault(field, message)
    if field_errors:
        raise ContentValidationError(field_errors)


def compile_and_validate(
    *,
    shared_caption: str,
    hashtags: Iterable[str],
    versions: Iterable[Any],
    content_format_version: int,
) -> list[CompiledPlatformContent]:
    compiled = compile_platform_contents(
        shared_caption=shared_caption,
        hashtags=hashtags,
        versions=versions,
        content_format_version=content_format_version,
    )
    validate_compiled_contents(compiled)
    return compiled


def validate_ai_candidate(candidate: dict, platforms: Iterable[str]) -> dict:
    normalized = normalize_hashtags(candidate.get("hashtags") or [])
    versions: list[dict] = []
    for platform in platforms:
        if platform == "youtube":
            versions.append(
                {
                    "platform": platform,
                    "caption": candidate.get("youtube_description") or "",
                    "title": candidate.get("youtube_title") or None,
                    "use_shared_caption": False,
                    "options": {"separate_caption": True},
                }
            )
        elif platform == "facebook":
            versions.append(
                {
                    "platform": platform,
                    "caption": candidate.get("facebook_caption") or "",
                    "title": candidate.get("facebook_title") or None,
                    "use_shared_caption": False,
                    "options": {"separate_caption": True},
                }
            )
        else:
            versions.append(
                {
                    "platform": platform,
                    "caption": candidate.get(f"{platform}_caption") or "",
                    "title": None,
                    "use_shared_caption": False,
                    "options": {"separate_caption": True},
                }
            )
    if "x_hashtags" in candidate:
        x_tags = normalize_hashtags(candidate["x_hashtags"])
        for version in versions:
            if version["platform"] == "x":
                version["options"].update(ai_hashtags=x_tags, ai_hashtags_source=normalized)
    compile_and_validate(
        shared_caption=candidate.get("shared_caption") or "",
        hashtags=normalized,
        versions=versions,
        content_format_version=2,
    )
    return {**candidate, "hashtags": normalized}
