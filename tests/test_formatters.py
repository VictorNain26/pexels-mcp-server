"""Formatter tests: minimal JSON projections + post-hoc dimension filter."""

from __future__ import annotations

import json

from pexels_mcp_server.formatters import (
    PhotoListResult,
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


# --- SDK convert_result patch regression ----------------------------------
# The MCP Python SDK 1.27 dumps tool results via ``model_dump(mode="json")``
# without ``exclude_unset=True``, leaking ``null`` for every optional
# TypedDict field. We patch ``FuncMetadata.convert_result`` to add the
# missing flag (see ``_sdk_patches.py``). This test guards both that the
# patch is applied and that it produces the right structured payload.


def _shape_probe() -> PhotoListResult:  # pragma: no cover - shape probe only
    return format_photo_list({})


def test_sdk_convert_result_patch_serialises_text_content_compact() -> None:
    """The SDK default serialises tool payloads with ``indent=2``, which
    is ~30 % larger than a compact dump. Our patch uses compact JSON
    (no whitespace, no newlines) for the text content while leaving the
    canonical payload in structuredContent. The text content carries
    the full payload — claude.ai's custom-connector path still feeds
    only ``content`` to the model, so a marker-only response would
    leave the agent hallucinating CDN patterns."""
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata
    from mcp.types import TextContent

    def _probe() -> PhotoListResult:  # pragma: no cover - shape probe only
        return format_photo_list({})

    meta = func_metadata(_probe)
    payload = {
        "page": 1,
        "per_page": 5,
        "photos": [
            {
                "id": 1,
                "alt": "x",
                "url": "https://pexels.com/photo/1",
                "photographer": "X",
                "photographer_url": "https://pexels.com/@x",
                "width": 4000,
                "height": 6000,
                "src": {"original": "https://images.pexels.com/photos/1/original.jpg"},
            }
        ],
    }
    converted = meta.convert_result(format_photo_list(payload))
    assert isinstance(converted, tuple), "Patched convert_result must return (content, structured)"
    unstructured, structured = converted

    # Content carries the FULL payload, but as compact JSON (no
    # indent, no newlines) — so the agent can read URLs directly.
    assert len(unstructured) == 1
    assert isinstance(unstructured[0], TextContent)
    text = unstructured[0].text
    assert "/original.jpg" in text, "Content must carry the actual image URL"
    assert "\n" not in text, "Compact JSON must not contain newlines"
    # Compact dump is significantly smaller than indented (~30 % saving).
    indented = json.dumps(structured, indent=2)
    assert len(text) < len(indented) * 0.85, (
        f"Compact text ({len(text)}c) should be much shorter than indented ({len(indented)}c)"
    )

    # Structured content stays the canonical typed payload.
    assert structured["photos"][0]["image_url"].endswith("/original.jpg")


def test_sdk_convert_result_patch_omits_unset_optional_typeddict_fields() -> None:
    """End-to-end check: the patched ``convert_result`` must not leak
    ``filter_diagnostics``/``total_results``/``next_page`` as ``null``
    when the formatter never set them. Without the patch, calls fail
    with ``Output validation error: None is not of type 'object'``."""
    import jsonschema
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    meta = func_metadata(_shape_probe)
    assert meta.output_schema is not None

    payload = {
        "page": 1,
        "per_page": 5,
        "photos": [
            {
                "id": 1,
                "alt": "Eiffel",
                "url": "https://pexels.com/photo/1",
                "photographer": "X",
                "photographer_url": "https://pexels.com/@x",
                "width": 4000,
                "height": 6000,
                "src": {"original": "https://images.pexels.com/photos/1/original.jpg"},
            }
        ],
    }
    converted = meta.convert_result(format_photo_list(payload))
    assert isinstance(converted, tuple)
    _, structured = converted

    assert "filter_diagnostics" not in structured
    assert "total_results" not in structured
    assert "next_page" not in structured

    jsonschema.validate(instance=structured, schema=meta.output_schema)


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


# --- featured collections (metadata, not media) --------------------------


_FULL_COLLECTION = {
    "id": "abc123",
    "title": "Nature",
    "description": "Green and serene scenes.",
    "private": False,
    "media_count": 200,
    "photos_count": 150,
    "videos_count": 50,
    # Unrelated fields Pexels might add later — should be ignored.
    "owner": "pexels",
    "created_at": "2024-01-01",
}


def test_collection_to_json_keeps_only_metadata_fields() -> None:
    from pexels_mcp_server.formatters import collection_to_json

    out = collection_to_json(_FULL_COLLECTION)
    assert set(out) == {
        "id",
        "title",
        "description",
        "private",
        "media_count",
        "photos_count",
        "videos_count",
    }
    assert "owner" not in out
    assert "created_at" not in out


def test_format_featured_collections_returns_minimal_envelope() -> None:
    from pexels_mcp_server.formatters import format_featured_collections

    payload = {
        "page": 1,
        "per_page": 15,
        "total_results": 30,
        "next_page": "https://api.pexels.com/v1/collections/featured?page=2",
        "collections": [_FULL_COLLECTION],
    }
    out = format_featured_collections(payload)
    assert out["total_results"] == 30
    assert out["has_more"] is True
    assert out["next_page"] == 2
    assert len(out["collections"]) == 1
    assert out["collections"][0]["id"] == "abc123"
