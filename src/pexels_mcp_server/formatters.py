"""Response formatters: turn raw Pexels payloads into JSON, Markdown, or rich
MCP content (text + inline images).

The JSON projections strip noisy fields (per-resolution URLs, OAuth flags) and
expose only the data agents typically reason about. The Markdown variants are
human-friendly summaries with the mandatory Pexels attribution footer. The
rich-content builders produce ``list[TextContent | ImageContent]`` so MCP
clients render thumbnails inline and vision-capable models can pick visually.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import ImageContent, TextContent

from .constants import PEXELS_ATTRIBUTION
from .previews import PreviewImage

_ATTRIBUTION_FOOTER = f"\n\n---\n{PEXELS_ATTRIBUTION}\n"


def _safe(value: Any, default: str = "") -> str:
    return str(value) if value is not None else default


def photo_to_json(photo: dict[str, Any]) -> dict[str, Any]:
    """Project a Pexels photo onto a token-lean JSON object.

    Returns only the high-signal fields an agent typically needs: page URL,
    alt text, photographer credit, dimensions, and two image URLs (full-res
    + a medium thumbnail). Discards ``liked``, ``photographer_id``,
    ``avg_color`` and the four per-orientation src variants that the agent
    rarely uses.
    """
    src = photo.get("src") or {}
    return {
        "id": photo.get("id"),
        "alt": photo.get("alt"),
        "page_url": photo.get("url"),
        "photographer": photo.get("photographer"),
        "photographer_url": photo.get("photographer_url"),
        "width": photo.get("width"),
        "height": photo.get("height"),
        "image_url": src.get("original"),
        "thumbnail_url": src.get("medium"),
    }


def photo_to_markdown(photo: dict[str, Any]) -> str:
    """One-paragraph human summary for a photo. For human inspection only."""
    src = photo.get("src") or {}
    return (
        f"- **#{_safe(photo.get('id'))}** "
        f"{_safe(photo.get('alt'), '(no alt)')} "
        f"by [{_safe(photo.get('photographer'))}]({_safe(photo.get('photographer_url'))}) "
        f"({_safe(photo.get('width'))}x{_safe(photo.get('height'))}) "
        f"-> {_safe(src.get('original'))}"
    )


_VIDEO_FILE_KEEP_LIMIT = 3


def _best_video_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick the top N files by resolution to keep token usage bounded.

    Pexels often returns 6-12 files per video (HLS + many resolutions). The
    agent typically only needs one or two streamable URLs.
    """
    sorted_files = sorted(
        files,
        key=lambda vf: (vf.get("width") or 0) * (vf.get("height") or 0),
        reverse=True,
    )
    return sorted_files[:_VIDEO_FILE_KEEP_LIMIT]


def video_to_json(video: dict[str, Any]) -> dict[str, Any]:
    """Project a Pexels video onto a token-lean JSON object.

    Returns high-signal fields and only the top 3 video_files by resolution.
    Discards ``video_pictures``, ``avg_color``, ``tags``, ``full_res`` and
    the ``user.id`` field (replaced by the more useful ``uploader_name``).
    """
    user = video.get("user") or {}
    files = video.get("video_files") or []
    top_files = _best_video_files(files)
    return {
        "id": video.get("id"),
        "page_url": video.get("url"),
        "duration_seconds": video.get("duration"),
        "width": video.get("width"),
        "height": video.get("height"),
        "preview_image_url": video.get("image"),
        "uploader_name": user.get("name"),
        "uploader_url": user.get("url"),
        "files": [
            {
                "quality": vf.get("quality"),
                "file_type": vf.get("file_type"),
                "width": vf.get("width"),
                "height": vf.get("height"),
                "fps": vf.get("fps"),
                "url": vf.get("link"),
            }
            for vf in top_files
        ],
        "total_files_available": len(files),
    }


def video_to_markdown(video: dict[str, Any]) -> str:
    """One-line human summary for a video. For human inspection only."""
    user = video.get("user") or {}
    return (
        f"- **#{_safe(video.get('id'))}** "
        f"{_safe(video.get('duration'))}s "
        f"{_safe(video.get('width'))}x{_safe(video.get('height'))} "
        f"by [{_safe(user.get('name'))}]({_safe(user.get('url'))}) "
        f"-> {_safe(video.get('url'))}"
    )


def collection_to_json(collection: dict[str, Any]) -> dict[str, Any]:
    """Project a Pexels collection onto a token-lean JSON object."""
    return {
        "id": collection.get("id"),
        "title": collection.get("title"),
        "description": collection.get("description"),
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
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
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
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
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
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
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
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    header = (
        f"**Pexels collection {_safe(payload.get('id'), '(unknown)')}** - "
        f"{_safe(total, '?')} total, "
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


# --------------------------------------------------------------- rich content
#
# The "rich" path returns a ``list[TextContent | ImageContent]`` so MCP
# clients that render multimodal content (claude.ai, Claude Desktop,
# Cursor, ChatGPT desktop) display the thumbnails inline. The first
# TextContent of every list is the same JSON / Markdown envelope the
# string-returning formatters above produce, so an agent that ignores
# images still has the full structured response it needs to paginate
# and pick.
#
# Per-item layout:
#   TextContent (caption: "#<id> alt — by <photographer> (WxH)")
#   ImageContent (the thumbnail, omitted on fetch failure)
#
# A missing preview degrades to caption-only; the caller still has the
# page URL and image URL in the JSON envelope to render manually.


_ContentList = list[TextContent | ImageContent]


def _text(text: str) -> TextContent:
    return TextContent(type="text", text=text)


def _image(preview: PreviewImage) -> ImageContent:
    return ImageContent(type="image", data=preview.data_base64, mimeType=preview.mime_type)


def _photo_caption(photo: dict[str, Any], idx: int) -> str:
    parts = [
        f"#{idx + 1} — id={_safe(photo.get('id'), 'unknown')}",
        _safe(photo.get("alt"), "(no alt)"),
        f"by {_safe(photo.get('photographer'), 'unknown')}",
        f"({_safe(photo.get('width'), '?')}x{_safe(photo.get('height'), '?')})",
    ]
    return " — ".join(parts)


def _video_caption(video: dict[str, Any], idx: int) -> str:
    user = video.get("user") or {}
    parts = [
        f"#{idx + 1} — id={_safe(video.get('id'), 'unknown')}",
        f"{_safe(video.get('duration'), '?')}s",
        f"{_safe(video.get('width'), '?')}x{_safe(video.get('height'), '?')}",
        f"by {_safe(user.get('name'), 'unknown')}",
    ]
    return " — ".join(parts)


def build_photo_list_rich(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    previews: list[PreviewImage | None],
    response_format: str,
) -> _ContentList:
    """Photo list as rich content. First block = JSON/Markdown envelope."""
    photos = payload.get("photos") or []
    envelope_str = format_photo_list(payload, rate_limit, response_format)
    out: _ContentList = [_text(envelope_str)]
    for i, photo in enumerate(photos):
        out.append(_text(_photo_caption(photo, i)))
        preview = previews[i] if i < len(previews) else None
        if preview is not None:
            out.append(_image(preview))
    return out


def build_video_list_rich(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    previews: list[PreviewImage | None],
    response_format: str,
) -> _ContentList:
    """Video list as rich content. Thumbnails come from the ``image`` field."""
    videos = payload.get("videos") or []
    envelope_str = format_video_list(payload, rate_limit, response_format)
    out: _ContentList = [_text(envelope_str)]
    for i, video in enumerate(videos):
        out.append(_text(_video_caption(video, i)))
        preview = previews[i] if i < len(previews) else None
        if preview is not None:
            out.append(_image(preview))
    return out


def build_single_photo_rich(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    preview: PreviewImage | None,
    response_format: str,
) -> _ContentList:
    """Single photo lookup as rich content."""
    envelope_str = format_single_photo(payload, rate_limit, response_format)
    out: _ContentList = [_text(envelope_str)]
    if preview is not None:
        out.append(_image(preview))
    return out


def build_single_video_rich(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    preview: PreviewImage | None,
    response_format: str,
) -> _ContentList:
    """Single video lookup as rich content. Thumbnail = the video's preview frame."""
    envelope_str = format_single_video(payload, rate_limit, response_format)
    out: _ContentList = [_text(envelope_str)]
    if preview is not None:
        out.append(_image(preview))
    return out


def build_collection_media_rich(
    payload: dict[str, Any],
    rate_limit: dict[str, Any] | None,
    previews: list[PreviewImage | None],
    response_format: str,
) -> _ContentList:
    """Collection media (mixed photos + videos) as rich content.

    The collection endpoint returns a single ``media`` list with ``type``
    discriminator (``Photo`` / ``Video``). Previews come from
    ``src.medium`` for photos and ``image`` for videos. The caller is
    responsible for resolving the right URL per item; this function
    just pairs each media slot with its (possibly missing) preview.
    """
    media = payload.get("media") or []
    envelope_str = format_collection_media(payload, rate_limit, response_format)
    out: _ContentList = [_text(envelope_str)]
    for i, item in enumerate(media):
        if item.get("type") == "Video":
            caption = _video_caption(item, i)
        else:
            caption = _photo_caption(item, i)
        out.append(_text(caption))
        preview = previews[i] if i < len(previews) else None
        if preview is not None:
            out.append(_image(preview))
    return out


def photo_preview_url(photo: dict[str, Any]) -> str | None:
    """Pick the canonical thumbnail URL for a Pexels photo payload.

    ``src.medium`` is the right trade-off: ~350x350 JPEG at ~30-100 KB,
    enough for a vision model to assess the image, light enough that
    fetching 15 in parallel stays under a second.
    """
    src = photo.get("src") or {}
    medium = src.get("medium")
    if isinstance(medium, str) and medium:
        return medium
    return None


def video_preview_url(video: dict[str, Any]) -> str | None:
    """Pick the canonical preview frame URL for a Pexels video payload.

    Pexels exposes the still preview at ``image``; the various
    ``video_pictures[].picture`` URLs are storyboard frames, not the
    canonical thumbnail.
    """
    image = video.get("image")
    if isinstance(image, str) and image:
        return image
    return None


def collection_item_preview_url(item: dict[str, Any]) -> str | None:
    """Pick the thumbnail URL for a collection-media item (Photo or Video)."""
    if item.get("type") == "Video":
        return video_preview_url(item)
    return photo_preview_url(item)


# --------------------------------------------------------- post-hoc filters
#
# Pexels' REST API exposes very coarse filters: ``size`` is a 3-bucket
# enum (large/medium/small) and there is no aspect-ratio filter. Marketing
# work needs both (Instagram 1:1, Story 9:16, hero 16:9, LinkedIn 4:5,
# print 4000+ px). We apply these as post-hoc filters on the items the
# REST call returned, since every Pexels item already carries native
# ``width`` and ``height``.


def filter_by_dimensions(
    items: list[dict[str, Any]],
    *,
    min_width: int | None = None,
    min_height: int | None = None,
    aspect_ratio: float | None = None,
    aspect_ratio_tolerance: float = 0.05,
) -> list[dict[str, Any]]:
    """Keep items matching the dimension / aspect-ratio constraints.

    Items lacking valid integer ``width``/``height`` are dropped silently
    — the alternative (keeping them) would let unbounded data through a
    filter the caller specifically asked for.
    """
    out: list[dict[str, Any]] = []
    for item in items:
        width = item.get("width")
        height = item.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            continue
        if width <= 0 or height <= 0:
            continue
        if min_width is not None and width < min_width:
            continue
        if min_height is not None and height < min_height:
            continue
        if aspect_ratio is not None:
            actual = width / height
            if abs(actual - aspect_ratio) > aspect_ratio * aspect_ratio_tolerance:
                continue
        out.append(item)
    return out
