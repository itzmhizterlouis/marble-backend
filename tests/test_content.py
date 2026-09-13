import pytest

from app.content import (
    ContentValidationError,
    compile_and_validate,
    compile_platform_contents,
    normalize_hashtags,
    utf8_bytes,
    utf16_units,
    validate_ai_candidate,
)


def version(platform: str, caption: str = "Body", title: str | None = None, *, separate=False):
    return {
        "platform": platform,
        "caption": caption,
        "title": title,
        "options": {"separate_caption": separate},
    }


def test_normalizes_and_deduplicates_hashtags():
    assert normalize_hashtags(["#Reverb", "reverb", "##Creators"]) == [
        "Reverb",
        "Creators",
    ]


def test_rejects_malformed_hashtags():
    with pytest.raises(ContentValidationError) as raised:
        normalize_hashtags(["creator-life"])
    assert raised.value.field_errors["hashtags.0"] == "Use letters, numbers or underscores"


def test_compiler_applies_global_hashtags_to_shared_and_separate_captions():
    compiled = compile_platform_contents(
        shared_caption="Shared body",
        hashtags=["Reverb", "Creators"],
        versions=[
            version("tiktok"),
            version("instagram", "Instagram body", separate=True),
        ],
        content_format_version=2,
    )
    assert compiled[0].caption == "Shared body #Reverb #Creators"
    assert compiled[1].caption == "Instagram body #Reverb #Creators"


def test_legacy_compiler_does_not_append_hashtags_again():
    compiled = compile_platform_contents(
        shared_caption="Shared body",
        hashtags=["Reverb"],
        versions=[version("instagram", "Legacy body #Reverb")],
        content_format_version=1,
    )
    assert compiled[0].caption == "Legacy body #Reverb"


@pytest.mark.parametrize(
    ("platform", "caption", "expected"),
    [
        ("tiktok", "😀" * 1101, "UTF-16"),
        ("instagram", "a" * 2201, "characters"),
        ("youtube", "😀" * 1251, "UTF-8"),
        ("facebook", "a" * 63207, "characters"),
    ],
)
def test_platform_caption_limits(platform, caption, expected):
    title = "YouTube title" if platform == "youtube" else None
    with pytest.raises(ContentValidationError) as raised:
        compile_and_validate(
            shared_caption=caption,
            hashtags=[],
            versions=[version(platform, title=title)],
            content_format_version=2,
        )
    assert expected in raised.value.field_errors[f"versions.{platform}.caption"]


def test_youtube_title_and_instagram_prohibited_hashtag_validation():
    with pytest.raises(ContentValidationError) as raised:
        compile_and_validate(
            shared_caption="Post",
            hashtags=["workflow"],
            versions=[version("instagram"), version("youtube", title="x" * 101)],
            content_format_version=2,
        )
    assert "#workflow" in raised.value.field_errors["versions.instagram.caption"]
    assert "Reduce by 1" in raised.value.field_errors["versions.youtube.title"]


def test_facebook_title_is_optional_but_bounded():
    compile_and_validate(
        shared_caption="Facebook body",
        hashtags=[],
        versions=[version("facebook")],
        content_format_version=2,
    )
    with pytest.raises(ContentValidationError) as raised:
        compile_and_validate(
            shared_caption="Facebook body",
            hashtags=[],
            versions=[version("facebook", title="x" * 256)],
            content_format_version=2,
        )
    assert "Reduce by 1" in raised.value.field_errors["versions.facebook.title"]


def test_facebook_title_is_required_when_youtube_is_in_the_same_request():
    with pytest.raises(ContentValidationError) as raised:
        compile_and_validate(
            shared_caption="Shared body",
            hashtags=[],
            versions=[
                version("facebook"),
                version("youtube", title="YouTube title"),
            ],
            content_format_version=2,
        )
    assert "with YouTube" in raised.value.field_errors["versions.facebook.title"]


def test_ai_candidate_is_normalized_and_validated():
    candidate = {
        "shared_caption": "Shared",
        "hashtags": ["#Reverb", "reverb"],
        "tiktok_caption": "TikTok",
        "instagram_caption": "Instagram",
        "facebook_title": "Facebook title",
        "facebook_caption": "Facebook",
        "youtube_title": "YouTube title",
        "youtube_description": "YouTube",
    }
    result = validate_ai_candidate(candidate, ["instagram", "facebook", "youtube"])
    assert result["hashtags"] == ["Reverb"]


def test_encoding_helpers_match_contract():
    assert utf16_units("😀") == 2
    assert utf8_bytes("😀") == 4
