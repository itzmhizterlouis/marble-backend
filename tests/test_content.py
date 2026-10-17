import pytest

from app.content import (
    ContentValidationError,
    compile_and_validate,
    compile_platform_contents,
    count_hashtags,
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


@pytest.mark.parametrize(("text", "count"), [
    ("#one #one #ONE", 3),
    ("(#one),#two!\n#three", 3),
    ("#café #東京 #e\u0301lan #tag_name", 4),
    ("# Heading ##heading ###title C# foo#bar https://example.com/#anchor", 0),
    ("#123 #_tag #😀 #", 2),
    ("#one#two", 1),
])
def test_hashtag_occurrences_and_boundaries(text, count):
    assert count_hashtags(text) == count


@pytest.mark.parametrize("count", [59, 60, 61])
def test_youtube_counts_title_caption_and_hashtag_field(count):
    # The same occurrence in the title and body still counts twice.
    body = " ".join(["#repeat"] * (count - 2))
    kwargs = dict(
        shared_caption=body, hashtags=["field"],
        versions=[version("youtube", title="Title #repeat")], content_format_version=2,
    )
    if count <= 60:
        result = compile_and_validate(**kwargs)
        assert result[0].caption == f"{body} #field"
    else:
        with pytest.raises(ContentValidationError) as raised:
            compile_and_validate(**kwargs)
        assert raised.value.field_errors["versions.youtube.caption"] == (
            "You have 61 hashtags across your title and description. "
            "Remove 1; YouTube ignores all hashtags above 60."
        )


def test_only_selected_youtube_final_copy_gets_youtube_limit():
    long_body = " ".join(f"#tag{i}" for i in range(61))
    result = compile_and_validate(
        shared_caption=long_body, hashtags=[], content_format_version=2,
        versions=[version("tiktok"), version("youtube", "Custom #short", "Title", separate=True)],
    )
    assert result[0].caption == long_body
    assert result[1].caption == "Custom #short"
    with pytest.raises(ContentValidationError):
        compile_and_validate(
            shared_caption="Short", hashtags=[], content_format_version=2,
            versions=[version("youtube", long_body, "Title", separate=True)],
        )


def test_legacy_hashtags_are_counted_from_rendered_copy_once():
    body = " ".join(f"#tag{i}" for i in range(60))
    result = compile_and_validate(
        shared_caption="Unused", hashtags=["not_appended"], content_format_version=1,
        versions=[version("youtube", body, "Title")],
    )
    assert result[0].caption == body
    with pytest.raises(ContentValidationError):
        compile_and_validate(
            shared_caption="Unused", hashtags=[], content_format_version=1,
            versions=[version("youtube", body + " #extra", "Title")],
        )


def test_ai_rejects_combined_hashtag_overflow():
    with pytest.raises(ContentValidationError) as raised:
        validate_ai_candidate({
            "youtube_title": "Title #one",
            "youtube_description": " ".join(f"#tag{i}" for i in range(59)),
            "hashtags": ["field"],
        }, ["youtube"])
    assert "61 hashtags" in raised.value.field_errors["versions.youtube.caption"]


@pytest.mark.parametrize(("body", "expected"), [
    ("a" * 280, 280), ("東" * 140, 280), ("👨‍👩‍👧‍👦" * 140, 280),
    ("cafe\u0301", 4), ("https://example.com/a/long/path", 23),
])
def test_x_uses_twitter_text_weighting(body, expected):
    from app.content import x_caption_units
    assert x_caption_units(body) == expected


@pytest.mark.parametrize(("platform", "body", "valid"), [
    ("x", "a" * 280, True), ("x", "a" * 281, False),
    ("x", "東" * 141, False), ("x", "👨‍👩‍👧‍👦" * 140, True),
    ("linkedin", "a" * 3000, True), ("linkedin", "a" * 3001, False),
    ("linkedin", "😀" * 1501, False),
])
def test_new_platform_caption_limits(platform, body, valid):
    kwargs = dict(shared_caption=body, hashtags=[], versions=[version(platform)], content_format_version=2)
    if valid:
        assert compile_and_validate(**kwargs)[0].caption == body
    else:
        with pytest.raises(ContentValidationError):
            compile_and_validate(**kwargs)


def test_new_platform_limits_include_hashtag_field():
    with pytest.raises(ContentValidationError):
        compile_and_validate(shared_caption="a" * 279, hashtags=["tag"], versions=[version("x")], content_format_version=2)


@pytest.mark.parametrize(("platform", "changes"), [
    ("linkedin", {"size_bytes": 74000}), ("linkedin", {"duration_seconds": 2}),
    ("linkedin", {"width": 100}), ("x", {"size_bytes": 8_000_000_001}),
    ("x", {"duration_seconds": 1201}),
])
def test_video_metadata_blocks_unsupported_destinations(platform, changes):
    from types import SimpleNamespace

    from app.content import validate_video_platforms
    values = dict(size_bytes=100000, duration_seconds=12, width=1080, height=1920)
    validate_video_platforms(SimpleNamespace(**values), ["x", "linkedin"])
    values.update(changes)
    with pytest.raises(ContentValidationError):
        validate_video_platforms(SimpleNamespace(**values), [platform])


@pytest.mark.parametrize(("width", "height"), [(3840, 2160), (2160, 3840), (1920, 400), (16, 16)])
def test_x_geometry_is_delegated_to_provider_for_all_publish_paths(width, height):
    from types import SimpleNamespace

    from app.content import validate_video_platforms
    from app.tasks import compile_post_content

    media = SimpleNamespace(size_bytes=100000, duration_seconds=12, width=width, height=height)
    validate_video_platforms(media, ["x"])
    post = SimpleNamespace(media=media, caption="My video", hashtags=[], content_format_version=2)
    assert compile_post_content(post, [SimpleNamespace(**version("x"))])[0].platform == "x"
