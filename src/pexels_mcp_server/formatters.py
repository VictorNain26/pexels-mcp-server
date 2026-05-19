"""JSON projections for Pexels REST payloads.

Every tool returns a ``dict`` so FastMCP populates both ``structuredContent``
(machine-readable, validated against the tool's ``outputSchema``) and a
TextContent block with the serialized JSON (backwards-compat for clients
that don't yet read structured output).

The shape stays deliberately minimal: every field exposed has a clear
purpose for the LLM (alt for filtering, image_url for the download link
the agent hands back to the user, photographer + photographer_url for the
attribution line Pexels licence requires). No thumbnail variants, no
rate-limit chrome, no narrative captions.
"""

from __future__ import annotations

from typing import Any


def photo_to_json(photo: dict[str, Any]) -> dict[str, Any]:
    """Project a Pexels photo to the minimal LLM-actionable shape."""
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
    }


def video_to_json(video: dict[str, Any]) -> dict[str, Any]:
    """Project a Pexels video, keeping only the top file by resolution."""
    user = video.get("user") or {}
    files = video.get("video_files") or []
    top = max(
        files,
        key=lambda vf: (vf.get("width") or 0) * (vf.get("height") or 0),
        default=None,
    )
    return {
        "id": video.get("id"),
        "page_url": video.get("url"),
        "duration_seconds": video.get("duration"),
        "width": video.get("width"),
        "height": video.get("height"),
        "uploader_name": user.get("name"),
        "uploader_url": user.get("url"),
        "video_url": top.get("link") if top else None,
        "quality": top.get("quality") if top else None,
    }


def _envelope(
    payload: dict[str, Any],
    *,
    items_key: str,
    items: list[dict[str, Any]],
    media_projector: Any,
) -> dict[str, Any]:
    """Common envelope wrapping a paginated payload.

    Carries the diagnostic block (`filter_diagnostics`) only when the tool
    layer attached one — i.e. when a post-hoc filter wiped every candidate
    and the agent needs a hint to retry without aspect_ratio.
    """
    page = int(payload.get("page", 1))
    per_page = int(payload.get("per_page", len(items)))
    total = payload.get("total_results")
    next_page_url = payload.get("next_page")
    has_more = bool(next_page_url)
    out: dict[str, Any] = {
        "page": page,
        "per_page": per_page,
        "count": len(items),
        "has_more": has_more,
        items_key: [media_projector(item) for item in items],
    }
    if total is not None:
        out["total_results"] = total
    if has_more:
        out["next_page"] = page + 1
    diagnostics = payload.get("filter_diagnostics")
    if diagnostics:
        out["filter_diagnostics"] = diagnostics
    return out


def format_photo_list(payload: dict[str, Any]) -> dict[str, Any]:
    return _envelope(
        payload,
        items_key="photos",
        items=payload.get("photos") or [],
        media_projector=photo_to_json,
    )


def format_video_list(payload: dict[str, Any]) -> dict[str, Any]:
    return _envelope(
        payload,
        items_key="videos",
        items=payload.get("videos") or [],
        media_projector=video_to_json,
    )


def format_collection_media(payload: dict[str, Any]) -> dict[str, Any]:
    media = payload.get("media") or []
    photos = [m for m in media if m.get("type") == "Photo"]
    videos = [m for m in media if m.get("type") == "Video"]
    page = int(payload.get("page", 1))
    per_page = int(payload.get("per_page", len(media)))
    total = payload.get("total_results")
    next_page_url = payload.get("next_page")
    has_more = bool(next_page_url)
    out: dict[str, Any] = {
        "id": payload.get("id"),
        "page": page,
        "per_page": per_page,
        "count": len(media),
        "has_more": has_more,
        "photos": [photo_to_json(p) for p in photos],
        "videos": [video_to_json(v) for v in videos],
    }
    if total is not None:
        out["total_results"] = total
    if has_more:
        out["next_page"] = page + 1
    diagnostics = payload.get("filter_diagnostics")
    if diagnostics:
        out["filter_diagnostics"] = diagnostics
    return out


def format_single_photo(payload: dict[str, Any]) -> dict[str, Any]:
    return {"photo": photo_to_json(payload)}


def format_single_video(payload: dict[str, Any]) -> dict[str, Any]:
    return {"video": video_to_json(payload)}


# --------------------------------------------------------- post-hoc filter


def filter_by_dimensions(
    items: list[dict[str, Any]],
    *,
    min_width: int | None = None,
    min_height: int | None = None,
    aspect_ratio: float | None = None,
    aspect_ratio_tolerance: float = 0.05,
) -> list[dict[str, Any]]:
    """Keep items matching the dimension / aspect-ratio constraints.

    Items lacking valid integer width/height are dropped silently — the
    alternative would let unbounded data slip through a filter the caller
    explicitly asked for.
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
