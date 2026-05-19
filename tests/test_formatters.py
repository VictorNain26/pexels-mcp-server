"""Formatter tests: validate the token-lean JSON projections, the rich
content builders that ship MCP ``ImageContent`` blocks alongside the JSON
envelope, and the post-hoc dimension filter used by every list tool."""

from __future__ import annotations

import json

from mcp.types import ImageContent, TextContent

from pexels_mcp_server.formatters import (
    build_collection_media_rich,
    build_photo_list_rich,
    build_single_photo_rich,
    build_single_video_rich,
    build_video_list_rich,
    collection_item_preview_url,
    filter_by_dimensions,
    format_collection_media,
    format_photo_list,
    format_single_photo,
    format_video_list,
    photo_preview_url,
    photo_to_json,
    video_preview_url,
    video_to_json,
)
from pexels_mcp_server.previews import PreviewImage

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
        "large2x": "https://x/large2x.jpg",
        "large": "https://x/large.jpg",
        "medium": "https://x/medium.jpg",
        "small": "https://x/small.jpg",
        "portrait": "https://x/portrait.jpg",
        "landscape": "https://x/landscape.jpg",
        "tiny": "https://x/tiny.jpg",
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
    "tags": ["aerial", "city"],
    "user": {"id": 7, "name": "Bob", "url": "https://pexels.com/@bob"},
    "video_files": [
        {
            "id": 1,
            "quality": "hd",
            "file_type": "video/mp4",
            "width": 1280,
            "height": 720,
            "fps": 25.0,
            "link": "https://x/hd.mp4",
        },
        {
            "id": 2,
            "quality": "uhd",
            "file_type": "video/mp4",
            "width": 3840,
            "height": 2160,
            "fps": 25.0,
            "link": "https://x/uhd.mp4",
        },
        {
            "id": 3,
            "quality": "sd",
            "file_type": "video/mp4",
            "width": 640,
            "height": 360,
            "fps": 25.0,
            "link": "https://x/sd.mp4",
        },
        {
            "id": 4,
            "quality": "hls",
            "file_type": "application/x-mpegURL",
            "width": 1920,
            "height": 1080,
            "fps": 25.0,
            "link": "https://x/stream.m3u8",
        },
    ],
    "video_pictures": [{"id": 1, "nr": 0, "picture": "https://x/pic1.jpg"}],
}


def test_photo_to_json_drops_low_signal_fields() -> None:
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
        "thumbnail_url",
    }
    assert out["image_url"] == "https://x/orig.jpg"
    assert out["thumbnail_url"] == "https://x/medium.jpg"
    assert "liked" not in out
    assert "photographer_id" not in out
    assert "avg_color" not in out
    assert "src" not in out


def test_video_to_json_keeps_only_top_three_files_by_resolution() -> None:
    out = video_to_json(_FULL_VIDEO)
    assert out["uploader_name"] == "Bob"
    assert out["preview_image_url"] == "https://x/preview.jpg"
    assert out["total_files_available"] == 4
    qualities = [f["quality"] for f in out["files"]]
    # uhd (3840x2160) > hls (1920x1080) > hd (1280x720) > sd (640x360)
    assert qualities == ["uhd", "hls", "hd"]
    assert "avg_color" not in out
    assert "tags" not in out
    assert "video_pictures" not in out


def test_format_photo_list_json_envelope() -> None:
    payload = {
        "page": 2,
        "per_page": 1,
        "total_results": 100,
        "next_page": "https://api.pexels.com/v1/search?page=3",
        "photos": [_FULL_PHOTO],
    }
    rate = {"limit": 200, "remaining": 199, "reset": "2026-01-01T00:00:00+00:00"}
    raw = format_photo_list(payload, rate, "json")
    parsed = json.loads(raw)
    assert parsed["total_results"] == 100
    assert parsed["page"] == 2
    assert parsed["has_more"] is True
    assert parsed["next_page"] == 3
    assert parsed["rate_limit"]["remaining"] == 199
    assert len(parsed["photos"]) == 1
    assert parsed["photos"][0]["image_url"] == "https://x/orig.jpg"


def test_format_video_list_json_envelope() -> None:
    payload = {
        "page": 1,
        "per_page": 1,
        "total_results": 5,
        "videos": [_FULL_VIDEO],
    }
    raw = format_video_list(payload, {}, "json")
    parsed = json.loads(raw)
    assert parsed["has_more"] is False
    assert parsed["next_page"] is None
    assert parsed["videos"][0]["duration_seconds"] == 30


def test_format_single_photo_includes_attribution_in_markdown() -> None:
    md = format_single_photo(_FULL_PHOTO, None, "markdown")
    assert "Alice" in md
    assert "Photos provided by Pexels" in md


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
    raw = format_collection_media(payload, None, "json")
    parsed = json.loads(raw)
    assert parsed["id"] == "abc"
    assert len(parsed["photos"]) == 1
    assert len(parsed["videos"]) == 1


# --- preview URL pickers --------------------------------------------------


def test_photo_preview_url_returns_medium() -> None:
    assert photo_preview_url(_FULL_PHOTO) == "https://x/medium.jpg"


def test_photo_preview_url_returns_none_when_missing() -> None:
    assert photo_preview_url({"src": {}}) is None
    assert photo_preview_url({}) is None


def test_video_preview_url_returns_image_field() -> None:
    assert video_preview_url(_FULL_VIDEO) == "https://x/preview.jpg"


def test_video_preview_url_returns_none_when_missing() -> None:
    assert video_preview_url({}) is None


def test_collection_item_preview_url_dispatches_on_type() -> None:
    photo = {**_FULL_PHOTO, "type": "Photo"}
    video = {**_FULL_VIDEO, "type": "Video"}
    assert collection_item_preview_url(photo) == "https://x/medium.jpg"
    assert collection_item_preview_url(video) == "https://x/preview.jpg"


# --- rich content builders -----------------------------------------------


def _preview() -> PreviewImage:
    return PreviewImage(
        data_base64="AAAA",
        mime_type="image/jpeg",
        source_url="https://images.pexels.com/photos/1/a.jpeg",
    )


def test_build_photo_list_rich_interleaves_captions_and_images() -> None:
    payload = {
        "page": 1,
        "per_page": 2,
        "total_results": 2,
        "photos": [_FULL_PHOTO, _FULL_PHOTO],
    }
    blocks = build_photo_list_rich(payload, {}, [_preview(), _preview()], "json")
    # Layout: envelope text + (caption text + image) x 2 = 5 blocks total
    assert len(blocks) == 5
    assert isinstance(blocks[0], TextContent)
    assert "total_results" in blocks[0].text  # envelope JSON
    assert isinstance(blocks[1], TextContent)
    assert blocks[1].text.startswith("#1 — id=1")
    assert isinstance(blocks[2], ImageContent)
    assert blocks[2].data == "AAAA"
    assert blocks[2].mimeType == "image/jpeg"


def test_build_photo_list_rich_handles_missing_previews() -> None:
    """When a fetch fails, the caption ships without the ImageContent."""
    payload = {
        "page": 1,
        "per_page": 2,
        "total_results": 2,
        "photos": [_FULL_PHOTO, _FULL_PHOTO],
    }
    blocks = build_photo_list_rich(payload, {}, [_preview(), None], "json")
    # Photo 1: caption + image. Photo 2: caption only. Total: envelope + 3.
    assert len(blocks) == 4
    types = [type(b).__name__ for b in blocks]
    assert types == ["TextContent", "TextContent", "ImageContent", "TextContent"]


def test_build_photo_list_rich_handles_empty_photos() -> None:
    payload = {"page": 1, "per_page": 0, "total_results": 0, "photos": []}
    blocks = build_photo_list_rich(payload, {}, [], "json")
    assert len(blocks) == 1  # just the envelope


def test_build_video_list_rich_interleaves_captions_and_images() -> None:
    payload = {
        "page": 1,
        "per_page": 1,
        "total_results": 1,
        "videos": [_FULL_VIDEO],
    }
    blocks = build_video_list_rich(payload, {}, [_preview()], "json")
    assert len(blocks) == 3  # envelope + caption + image
    assert isinstance(blocks[1], TextContent)
    assert "30s" in blocks[1].text
    assert isinstance(blocks[2], ImageContent)


def test_build_single_photo_rich_with_preview() -> None:
    blocks = build_single_photo_rich(_FULL_PHOTO, {}, _preview(), "json")
    assert len(blocks) == 2
    assert isinstance(blocks[0], TextContent)
    assert isinstance(blocks[1], ImageContent)


def test_build_single_photo_rich_without_preview() -> None:
    blocks = build_single_photo_rich(_FULL_PHOTO, {}, None, "json")
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextContent)


def test_build_single_video_rich_with_preview() -> None:
    blocks = build_single_video_rich(_FULL_VIDEO, {}, _preview(), "json")
    assert len(blocks) == 2
    assert isinstance(blocks[1], ImageContent)


def test_build_collection_media_rich_dispatches_on_type() -> None:
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
    blocks = build_collection_media_rich(payload, {}, [_preview(), _preview()], "json")
    # envelope + (caption + image) x 2 = 5
    assert len(blocks) == 5
    # Photo caption mentions dimensions, video caption mentions seconds.
    assert "1920x1080" in blocks[1].text  # type: ignore[union-attr]
    assert "30s" in blocks[3].text  # type: ignore[union-attr]


def test_build_rich_preserves_envelope_for_agents() -> None:
    """The first TextContent must be a parseable JSON envelope so an agent
    that cannot render images still gets the full structured response."""
    payload = {
        "page": 1,
        "per_page": 1,
        "total_results": 1,
        "photos": [_FULL_PHOTO],
    }
    blocks = build_photo_list_rich(payload, {"remaining": 200}, [_preview()], "json")
    envelope = json.loads(blocks[0].text)  # type: ignore[union-attr]
    assert envelope["total_results"] == 1
    assert envelope["photos"][0]["id"] == 1
    assert envelope["rate_limit"]["remaining"] == 200


# --- filter_by_dimensions: post-hoc filter -------------------------------


def _item(width: int, height: int) -> dict:
    return {"id": 1, "width": width, "height": height}


def test_filter_by_dimensions_min_width_drops_too_small() -> None:
    items = [_item(1000, 1000), _item(2000, 1000), _item(500, 500)]
    out = filter_by_dimensions(items, min_width=1500)
    assert [i["width"] for i in out] == [2000]


def test_filter_by_dimensions_min_height_drops_too_short() -> None:
    items = [_item(2000, 800), _item(2000, 1080), _item(2000, 200)]
    out = filter_by_dimensions(items, min_height=1080)
    assert [i["height"] for i in out] == [1080]


def test_filter_by_dimensions_aspect_ratio_with_default_tolerance() -> None:
    """Default tolerance is 5%. 16:9 (~1.778) matches 1920x1080, rejects 4:3."""
    items = [
        _item(1920, 1080),  # 16:9 exactly
        _item(1600, 1200),  # 4:3 (1.333) — off by far, drop
        _item(1280, 720),  # 16:9 exactly
        _item(1900, 1080),  # ~1.759 — within 5% of 1.778
    ]
    out = filter_by_dimensions(items, aspect_ratio=16 / 9)
    assert len(out) == 3
    assert _item(1600, 1200) not in out


def test_filter_by_dimensions_aspect_ratio_tolerance_override() -> None:
    """Tight tolerance only keeps near-perfect matches."""
    items = [_item(1920, 1080), _item(1900, 1080)]
    # 1900/1080 ≈ 1.759 vs target 1.778 → off by ~1%. With tol=0.005 (0.5%), drop.
    out = filter_by_dimensions(items, aspect_ratio=16 / 9, aspect_ratio_tolerance=0.005)
    assert out == [_item(1920, 1080)]


def test_filter_by_dimensions_combines_all_constraints() -> None:
    items = [
        _item(1920, 1080),  # OK: 16:9 + min_width=1920 + min_height=1080
        _item(1280, 720),  # KO: width < 1920
        _item(1920, 1440),  # KO: 4:3, not 16:9
        _item(2560, 1440),  # OK: 16:9 + min_width OK + min_height OK
    ]
    out = filter_by_dimensions(items, min_width=1920, min_height=1080, aspect_ratio=16 / 9)
    assert [i["width"] for i in out] == [1920, 2560]


def test_filter_by_dimensions_drops_items_without_dims() -> None:
    """An item missing width/height is dropped under any filter so unbounded
    data does not slip through a filter the caller asked for."""
    items = [
        {"id": 1, "width": 1920, "height": 1080},
        {"id": 2},  # no dims
        {"id": 3, "width": "1920", "height": "1080"},  # wrong type
        {"id": 4, "width": -10, "height": 1080},  # bad value
    ]
    out = filter_by_dimensions(items, min_width=1)
    assert [i["id"] for i in out] == [1]


def test_filter_by_dimensions_noop_when_no_filters() -> None:
    items = [_item(100, 100), _item(200, 200)]
    out = filter_by_dimensions(items)
    assert out == items
