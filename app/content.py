from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

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
        if content_format_version >= 2:
            use_shared = _value(version, "use_shared_caption")
            separate = (
                not use_shared
                if use_shared is not None
                else options.get("separate_caption") is True
            )
            body = caption if separate else shared_caption
            caption = compose_caption(body, normalized_hashtags)
        else:
            caption = caption.strip()
        compiled.append(
            CompiledPlatformContent(
                platform=platform,
                caption=caption,
                title=title,
                hashtags=normalized_hashtags,
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
        elif platform == "facebook":
            count = len(content.caption)
            if count > 63206:
                field_errors[caption_field] = f"Reduce by {count - 63206} characters"
            if content.title and len(content.title) > 255:
                field_errors[title_field] = f"Reduce by {len(content.title) - 255} characters"
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
    compile_and_validate(
        shared_caption=candidate.get("shared_caption") or "",
        hashtags=normalized,
        versions=versions,
        content_format_version=2,
    )
    return {**candidate, "hashtags": normalized}
