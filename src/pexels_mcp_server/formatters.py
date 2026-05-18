"""Response formatters: turn raw Pexels payloads into JSON or Markdown strings.

The JSON projections strip noisy fields (per-resolution URLs, OAuth flags) and
expose only the data agents typically reason about. The Markdown variants are
human-friendly summaries with the mandatory Pexels attribution footer.
"""

from __future__ import annotations

import json
from typing import Any

from .constants import PEXELS_ATTRIBUTION

_ATTRIBUTION_FOOTER = f"\n\n---\n{PEXELS_ATTRIBUTION}\n"


def _safe(value: Any, default: str = "") -> str:
    return str(value) if value is not None else default


def photo_to_json(photo: dict[str, Any]) -> dict[str, Any]:
    """Project a Pexels photo onto a lean JSON object."""
    src = photo.get("src") or {}
    return {
        "id": photo.get("id"),
        "alt": photo.get("alt"),
        "url": photo.get("url"),
        "photographer": photo.get("photographer"),
        "photographer_url": photo.get("photographer_url"),
        "avg_color": photo.get("avg_color"),
        "dimensions": {
            "width": photo.get("width"),
            "height": photo.get("height"),
        },
        "src": {
            "original": src.get("original"),
            "large": src.get("large"),
            "medium": src.get("medium"),
            "small": src.get("small"),
            "portrait": src.get("portrait"),
            "landscape": src.get("landscape"),
        },
    }


def photo_to_markdown(photo: dict[str, Any]) -> str:
    """One-paragraph human summary for a photo."""
    src = photo.get("src") or {}
    lines = [
        f"### Photo #{_safe(photo.get('id'))}",
        f"- Alt: {_safe(photo.get('alt'), '(no alt text)')}",
        f"- Photographer: [{_safe(photo.get('photographer'))}]({_safe(photo.get('photographer_url'))})",
        f"- Dimensions: {_safe(photo.get('width'))}x{_safe(photo.get('height'))}",
        f"- Page: {_safe(photo.get('url'))}",
        f"- Original: {_safe(src.get('original'))}",
    ]
    return "\n".join(lines)


def video_to_json(video: dict[str, Any]) -> dict[str, Any]:
    """Project a Pexels video onto a lean JSON object."""
    user = video.get("user") or {}
    video_files = video.get("video_files") or []
    return {
        "id": video.get("id"),
        "url": video.get("url"),
        "duration_seconds": video.get("duration"),
        "dimensions": {
            "width": video.get("width"),
            "height": video.get("height"),
        },
        "preview_image": video.get("image"),
        "user": {
            "id": user.get("id"),
            "name": user.get("name"),
            "url": user.get("url"),
        },
        "video_files": [
            {
                "quality": vf.get("quality"),
                "file_type": vf.get("file_type"),
                "width": vf.get("width"),
                "height": vf.get("height"),
                "fps": vf.get("fps"),
                "link": vf.get("link"),
            }
            for vf in video_files
        ],
    }


def video_to_markdown(video: dict[str, Any]) -> str:
    """One-paragraph human summary for a video."""
    user = video.get("user") or {}
    files = video.get("video_files") or []
    qualities = (
        ", ".join(sorted({str(vf.get("quality")) for vf in files if vf.get("quality")})) or "(none)"
    )
    lines = [
        f"### Video #{_safe(video.get('id'))}",
        f"- Duration: {_safe(video.get('duration'))}s",
        f"- Dimensions: {_safe(video.get('width'))}x{_safe(video.get('height'))}",
        f"- Uploader: [{_safe(user.get('name'))}]({_safe(user.get('url'))})",
        f"- Page: {_safe(video.get('url'))}",
        f"- Available qualities: {qualities}",
        f"- Preview: {_safe(video.get('image'))}",
    ]
    return "\n".join(lines)


def collection_to_json(collection: dict[str, Any]) -> dict[str, Any]:
    """Project a Pexels collection onto a lean JSON object."""
    return {
        "id": collection.get("id"),
        "title": collection.get("title"),
        "description": collection.get("description"),
        "private": collection.get("private"),
        "media_count": collection.get("media_count"),
        "photos_count": collection.get("photos_count"),
        "videos_count": collection.get("videos_count"),
    }


def collection_to_markdown(collection: dict[str, Any]) -> str:
    """One-paragraph human summary for a collection."""
    lines = [
        f"### Collection {_safe(collection.get('id'))} - {_safe(collection.get('title'))}",
        f"- Description: {_safe(collection.get('description'), '(none)')}",
        f"- Media count: {_safe(collection.get('media_count'))} "
        f"(photos: {_safe(collection.get('photos_count'))}, "
        f"videos: {_safe(collection.get('videos_count'))})",
    ]
    return "\n".join(lines)


def _envelope(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    *,
    per_page_key: str = "per_page",
    items_key: str,
    items: list[dict[str, Any]],
    media_projector: Any,
) -> dict[str, Any]:
    """Common envelope wrapping the paginated payload + rate limit info."""
    page = int(payload.get("page", 1))
    per_page = int(payload.get(per_page_key, len(items)))
    total = payload.get("total_results")
    next_page_url = payload.get("next_page")
    has_more = bool(next_page_url)
    return {
        "total_results": total,
        "page": page,
        "per_page": per_page,
        "count": len(items),
        "has_more": has_more,
        "next_page": (page + 1) if has_more else None,
        "rate_limit": rate_limit or {},
        items_key: [media_projector(item) for item in items],
    }


def format_photo_list(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    response_format: str,
) -> str:
    """Format a paginated list of photos."""
    photos = payload.get("photos") or []
    envelope = _envelope(
        payload,
        rate_limit,
        items_key="photos",
        items=photos,
        media_projector=photo_to_json,
    )
    if response_format == "json":
        return json.dumps(envelope, indent=2, ensure_ascii=False)
    header = (
        f"**Pexels photos** - {envelope['total_results']} total, "
        f"page {envelope['page']} ({envelope['count']} shown)"
    )
    body = "\n\n".join(photo_to_markdown(p) for p in photos) or "_No results._"
    return f"{header}\n\n{body}{_ATTRIBUTION_FOOTER}"


def format_video_list(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    response_format: str,
) -> str:
    """Format a paginated list of videos."""
    videos = payload.get("videos") or []
    envelope = _envelope(
        payload,
        rate_limit,
        items_key="videos",
        items=videos,
        media_projector=video_to_json,
    )
    if response_format == "json":
        return json.dumps(envelope, indent=2, ensure_ascii=False)
    header = (
        f"**Pexels videos** - {envelope['total_results']} total, "
        f"page {envelope['page']} ({envelope['count']} shown)"
    )
    body = "\n\n".join(video_to_markdown(v) for v in videos) or "_No results._"
    return f"{header}\n\n{body}{_ATTRIBUTION_FOOTER}"


def format_collection_list(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    response_format: str,
) -> str:
    """Format a paginated list of collections."""
    collections = payload.get("collections") or []
    envelope = _envelope(
        payload,
        rate_limit,
        items_key="collections",
        items=collections,
        media_projector=collection_to_json,
    )
    if response_format == "json":
        return json.dumps(envelope, indent=2, ensure_ascii=False)
    header = (
        f"**Pexels collections** - {envelope['total_results']} total, "
        f"page {envelope['page']} ({envelope['count']} shown)"
    )
    body = "\n\n".join(collection_to_markdown(c) for c in collections) or "_No results._"
    return f"{header}\n\n{body}{_ATTRIBUTION_FOOTER}"


def format_collection_media(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    response_format: str,
) -> str:
    """Format the contents of a single collection (photos + videos mixed)."""
    media = payload.get("media") or []
    photos = [m for m in media if m.get("type") == "Photo"]
    videos = [m for m in media if m.get("type") == "Video"]
    page = int(payload.get("page", 1))
    per_page = int(payload.get("per_page", len(media)))
    total = payload.get("total_results")
    next_page_url = payload.get("next_page")
    has_more = bool(next_page_url)
    envelope = {
        "id": payload.get("id"),
        "total_results": total,
        "page": page,
        "per_page": per_page,
        "count": len(media),
        "has_more": has_more,
        "next_page": (page + 1) if has_more else None,
        "rate_limit": rate_limit or {},
        "photos": [photo_to_json(p) for p in photos],
        "videos": [video_to_json(v) for v in videos],
    }
    if response_format == "json":
        return json.dumps(envelope, indent=2, ensure_ascii=False)
    header = (
        f"**Pexels collection {payload.get('id')}** - {total} total, "
        f"page {page} ({envelope['count']} shown)"
    )
    photo_block = "\n\n".join(photo_to_markdown(p) for p in photos)
    video_block = "\n\n".join(video_to_markdown(v) for v in videos)
    body = "\n\n".join(part for part in (photo_block, video_block) if part) or "_No results._"
    return f"{header}\n\n{body}{_ATTRIBUTION_FOOTER}"


def format_single_photo(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    response_format: str,
) -> str:
    """Format a single photo lookup."""
    if response_format == "json":
        return json.dumps(
            {"photo": photo_to_json(payload), "rate_limit": rate_limit or {}},
            indent=2,
            ensure_ascii=False,
        )
    return f"{photo_to_markdown(payload)}{_ATTRIBUTION_FOOTER}"


def format_single_video(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    response_format: str,
) -> str:
    """Format a single video lookup."""
    if response_format == "json":
        return json.dumps(
            {"video": video_to_json(payload), "rate_limit": rate_limit or {}},
            indent=2,
            ensure_ascii=False,
        )
    return f"{video_to_markdown(payload)}{_ATTRIBUTION_FOOTER}"
