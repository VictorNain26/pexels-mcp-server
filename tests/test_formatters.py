"""Formatter tests: validate the token-lean JSON projections."""

from __future__ import annotations

import json

from pexels_mcp_server.formatters import (
    format_collection_media,
    format_photo_list,
    format_single_photo,
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
