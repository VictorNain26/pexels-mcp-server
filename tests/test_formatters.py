"""Formatter tests: minimal JSON projections + post-hoc dimension filter."""

from __future__ import annotations

from pexels_mcp_server.formatters import (
    filter_by_dimensions,
    format_collection_media,
    format_photo_list,
    format_single_photo,
    format_single_video,
    format_video_list,
    photo_to_json,
    video_to_json,
)

_FULL_PHOTO = {
    "id": 1,
    "width": 1920,
    "height": 1080,
    "url": "https://pexels.com/photo/test-1",
    "photographer": "Alice",
    "photographer_url": "https://pexels.com/@alice",
    "photographer_id": 99,
    "avg_color": "#444",
    "liked": True,
    "alt": "a sunny garden",
    "src": {
        "original": "https://x/orig.jpg",
        "medium": "https://x/medium.jpg",
    },
}

_FULL_VIDEO = {
    "id": 10,
    "width": 3840,
    "height": 2160,
    "duration": 30,
    "url": "https://pexels.com/video/test-10",
    "image": "https://x/preview.jpg",
    "avg_color": "#000",
    "user": {"id": 7, "name": "Bob", "url": "https://pexels.com/@bob"},
    "video_files": [
        {"quality": "hd", "width": 1280, "height": 720, "link": "https://x/hd.mp4"},
        {"quality": "uhd", "width": 3840, "height": 2160, "link": "https://x/uhd.mp4"},
        {"quality": "sd", "width": 640, "height": 360, "link": "https://x/sd.mp4"},
    ],
}


# --- minimal photo projection ---------------------------------------------


def test_photo_to_json_keeps_essential_fields_only() -> None:
    out = photo_to_json(_FULL_PHOTO)
    assert set(out) == {
        "id",
        "alt",
        "page_url",
        "photographer",
        "photographer_url",
        "width",
        "height",
        "image_url",
    }
    assert out["image_url"] == "https://x/orig.jpg"
    assert "thumbnail_url" not in out, (
        "thumbnail_url dropped: redundant with image_url (?h=350 works on either)"
    )
    assert "liked" not in out
    assert "photographer_id" not in out


def test_video_to_json_keeps_only_top_file() -> None:
    out = video_to_json(_FULL_VIDEO)
    assert out["uploader_name"] == "Bob"
    assert out["video_url"] == "https://x/uhd.mp4"  # highest resolution
    assert out["quality"] == "uhd"
    assert "preview_image_url" not in out, (
        "preview_image_url dropped: video_url is the actionable link"
    )
    assert "files" not in out
    assert "total_files_available" not in out


# --- envelope shapes ------------------------------------------------------


def test_format_photo_list_returns_minimal_envelope() -> None:
    payload = {
        "page": 2,
        "per_page": 1,
        "total_results": 100,
        "next_page": "https://api.pexels.com/v1/search?page=3",
        "photos": [_FULL_PHOTO],
    }
    out = format_photo_list(payload)
    assert out["total_results"] == 100
    assert out["page"] == 2
    assert out["has_more"] is True
    assert out["next_page"] == 3
    assert "rate_limit" not in out, "rate_limit dropped (kept server-side logging)"
    assert len(out["photos"]) == 1
    assert out["photos"][0]["image_url"] == "https://x/orig.jpg"


def test_format_photo_list_omits_next_page_when_last() -> None:
    payload = {
        "page": 5,
        "per_page": 1,
        "total_results": 5,
        "photos": [_FULL_PHOTO],
    }
    out = format_photo_list(payload)
    assert out["has_more"] is False
    assert "next_page" not in out


def test_format_video_list_returns_minimal_envelope() -> None:
    payload = {
        "page": 1,
        "per_page": 1,
        "total_results": 5,
        "videos": [_FULL_VIDEO],
    }
    out = format_video_list(payload)
    assert out["videos"][0]["duration_seconds"] == 30
    assert out["videos"][0]["video_url"] == "https://x/uhd.mp4"


def test_format_single_photo_wraps_projection_in_photo_key() -> None:
    out = format_single_photo(_FULL_PHOTO)
    assert out == {"photo": photo_to_json(_FULL_PHOTO)}


def test_format_single_video_wraps_projection_in_video_key() -> None:
    out = format_single_video(_FULL_VIDEO)
    assert out == {"video": video_to_json(_FULL_VIDEO)}


def test_format_collection_media_splits_photos_and_videos() -> None:
    payload = {
        "id": "abc",
        "page": 1,
        "per_page": 2,
        "total_results": 2,
        "media": [
            {**_FULL_PHOTO, "type": "Photo"},
            {**_FULL_VIDEO, "type": "Video"},
        ],
    }
    out = format_collection_media(payload)
    assert out["id"] == "abc"
    assert len(out["photos"]) == 1
    assert len(out["videos"]) == 1


# --- filter_diagnostics (only on actionable wipe) ------------------------


def test_envelope_surfaces_filter_diagnostics_only_when_wiped() -> None:
    """The diagnostic block is only emitted when post_filter_count == 0 and
    pre_filter_count > 0 — i.e. when the agent should retry without
    aspect_ratio. No noise on the happy path."""
    payload = {
        "page": 1,
        "per_page": 5,
        "total_results": 100,
        "photos": [_FULL_PHOTO],
        "filter_diagnostics": {
            "applied_filters": {"aspect_ratio": "16:9"},
            "pre_filter_count": 12,
            "post_filter_count": 0,
            "suggestion": "Filters rejected every candidate. Retry without aspect_ratio.",
        },
    }
    out = format_photo_list(payload)
    assert "filter_diagnostics" in out
    assert out["filter_diagnostics"]["post_filter_count"] == 0


def test_envelope_omits_filter_diagnostics_when_payload_has_none() -> None:
    payload = {
        "page": 1,
        "per_page": 5,
        "total_results": 100,
        "photos": [_FULL_PHOTO],
    }
    out = format_photo_list(payload)
    assert "filter_diagnostics" not in out


# --- Output-schema regression (MCP SDK 1.27 emits null for absent optional --
# --- TypedDict fields; the schema MUST accept that null) -----------------


def test_output_schema_accepts_null_for_optional_fields() -> None:
    """``model_dump(mode="json")`` in the MCP SDK does not pass
    ``exclude_unset=True``, so optional TypedDict fields land as ``null``
    in ``structuredContent``. Until the SDK is fixed, our TypedDicts
    declare optional fields as ``T | None`` so the generated jsonschema
    accepts that bogus ``null`` (otherwise the call fails with
    ``"None is not of type 'object'"``)."""
    import jsonschema
    from mcp.server.fastmcp.utilities.func_metadata import (
        _create_model_from_typeddict,
    )

    from pexels_mcp_server.formatters import (
        CollectionMediaResult,
        PhotoListResult,
        VideoListResult,
    )

    for typed_dict in (PhotoListResult, VideoListResult, CollectionMediaResult):
        pydantic_model = _create_model_from_typeddict(typed_dict)
        schema = pydantic_model.model_json_schema()
        # Build the same minimal envelope our formatters return when the
        # upstream Pexels payload carries none of the optional fields.
        minimal: dict[str, object] = {
            "page": 1,
            "per_page": 5,
            "count": 0,
            "has_more": False,
            "photos": [],
        }
        if typed_dict is VideoListResult:
            minimal = {**{k: v for k, v in minimal.items() if k != "photos"}, "videos": []}
        elif typed_dict is CollectionMediaResult:
            minimal = {**minimal, "id": None, "videos": []}
        validated = pydantic_model.model_validate(minimal)
        dumped = validated.model_dump(mode="json", by_alias=True)
        # The SDK injects null for unset optional fields; the schema MUST
        # accept that — otherwise the call fails with
        # ``Output validation error: None is not of type 'object'``.
        jsonschema.validate(instance=dumped, schema=schema)


# --- filter_by_dimensions: post-hoc filter -------------------------------


def _item(width: int, height: int) -> dict:
    return {"id": 1, "width": width, "height": height}


def test_filter_by_dimensions_min_width_drops_too_small() -> None:
    out = filter_by_dimensions(
        [_item(1000, 1000), _item(2000, 1000), _item(500, 500)],
        min_width=1500,
    )
    assert [i["width"] for i in out] == [2000]


def test_filter_by_dimensions_aspect_ratio_with_default_tolerance() -> None:
    items = [
        _item(1920, 1080),  # 16:9 exactly
        _item(1600, 1200),  # 4:3, drop
        _item(1280, 720),  # 16:9 exactly
    ]
    out = filter_by_dimensions(items, aspect_ratio=16 / 9)
    assert len(out) == 2


def test_filter_by_dimensions_aspect_ratio_tolerance_override() -> None:
    items = [_item(1920, 1080), _item(1900, 1080)]
    out = filter_by_dimensions(items, aspect_ratio=16 / 9, aspect_ratio_tolerance=0.005)
    assert out == [_item(1920, 1080)]


def test_filter_by_dimensions_drops_items_without_dims() -> None:
    items = [
        {"id": 1, "width": 1920, "height": 1080},
        {"id": 2},
        {"id": 3, "width": "1920", "height": "1080"},
        {"id": 4, "width": -10, "height": 1080},
    ]
    out = filter_by_dimensions(items, min_width=1)
    assert [i["id"] for i in out] == [1]


def test_filter_by_dimensions_noop_when_no_filters() -> None:
    items = [_item(100, 100), _item(200, 200)]
    assert filter_by_dimensions(items) == items
