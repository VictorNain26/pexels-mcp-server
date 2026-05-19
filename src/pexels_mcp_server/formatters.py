"""JSON projections for Pexels REST payloads.

The shape stays deliberately minimal: every field we expose has a clear
purpose for the LLM (alt for filtering, image_url for the download link
it will hand back to the user, photographer + photographer_url for the
attribution line Pexels licence requires). No thumbnail variants, no
rate-limit chrome, no narrative captions — the tool returns just enough
JSON for the agent to format the user-visible answer itself.
"""

from __future__ import annotations

import json
from typing import Any

from .constants import PEXELS_ATTRIBUTION

_ATTRIBUTION_FOOTER = f"\n\n---\n{PEXELS_ATTRIBUTION}\n"


def _safe(value: Any, default: str = "") -> str:
    return str(value) if value is not None else default


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


def format_photo_list(payload: dict[str, Any], response_format: str) -> str:
    photos = payload.get("photos") or []
    envelope = _envelope(payload, items_key="photos", items=photos, media_projector=photo_to_json)
    if response_format == "json":
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    header = f"**Pexels photos** — {envelope.get('total_results', '?')} total, page {envelope['page']} ({envelope['count']} shown)"
    body = (
        "\n".join(
            f"- #{_safe(p.get('id'))} — {_safe(p.get('alt'), '(no alt)')} — "
            f"by [{_safe(p.get('photographer'))}]({_safe(p.get('photographer_url'))}) "
            f"— {_safe(p.get('width'))}x{_safe(p.get('height'))} — {_safe(p.get('image_url'))}"
            for p in photos
        )
        or "_No results._"
    )
    return f"{header}\n\n{body}{_ATTRIBUTION_FOOTER}"


def format_video_list(payload: dict[str, Any], response_format: str) -> str:
    videos = payload.get("videos") or []
    envelope = _envelope(payload, items_key="videos", items=videos, media_projector=video_to_json)
    if response_format == "json":
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    header = f"**Pexels videos** — {envelope.get('total_results', '?')} total, page {envelope['page']} ({envelope['count']} shown)"
    body = (
        "\n".join(
            f"- #{_safe(v.get('id'))} — {_safe(v.get('duration'), '?')}s "
            f"{_safe(v.get('width'))}x{_safe(v.get('height'))} — "
            f"{_safe((v.get('user') or {}).get('name'))} — {_safe(v.get('url'))}"
            for v in videos
        )
        or "_No results._"
    )
    return f"{header}\n\n{body}{_ATTRIBUTION_FOOTER}"


def format_collection_media(payload: dict[str, Any], response_format: str) -> str:
    media = payload.get("media") or []
    photos = [m for m in media if m.get("type") == "Photo"]
    videos = [m for m in media if m.get("type") == "Video"]
    page = int(payload.get("page", 1))
    per_page = int(payload.get("per_page", len(media)))
    total = payload.get("total_results")
    next_page_url = payload.get("next_page")
    has_more = bool(next_page_url)
    envelope: dict[str, Any] = {
        "id": payload.get("id"),
        "page": page,
        "per_page": per_page,
        "count": len(media),
        "has_more": has_more,
        "photos": [photo_to_json(p) for p in photos],
        "videos": [video_to_json(v) for v in videos],
    }
    if total is not None:
        envelope["total_results"] = total
    if has_more:
        envelope["next_page"] = page + 1
    diagnostics = payload.get("filter_diagnostics")
    if diagnostics:
        envelope["filter_diagnostics"] = diagnostics
    if response_format == "json":
        return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    header = (
        f"**Pexels collection {_safe(payload.get('id'), '(unknown)')}** — "
        f"{_safe(total, '?')} total, page {page} ({envelope['count']} shown)"
    )
    lines: list[str] = []
    for p in photos:
        lines.append(
            f"- photo #{_safe(p.get('id'))} — {_safe(p.get('alt'), '(no alt)')} — "
            f"by {_safe(p.get('photographer'))}"
        )
    for v in videos:
        user = v.get("user") or {}
        lines.append(
            f"- video #{_safe(v.get('id'))} — {_safe(v.get('duration'))}s — by {_safe(user.get('name'))}"
        )
    body = "\n".join(lines) or "_No results._"
    return f"{header}\n\n{body}{_ATTRIBUTION_FOOTER}"


def format_single_photo(payload: dict[str, Any], response_format: str) -> str:
    if response_format == "json":
        return json.dumps({"photo": photo_to_json(payload)}, ensure_ascii=False)
    p = photo_to_json(payload)
    return (
        f"- #{_safe(p['id'])} — {_safe(p['alt'], '(no alt)')} — "
        f"by [{_safe(p['photographer'])}]({_safe(p['photographer_url'])}) — "
        f"{_safe(p['width'])}x{_safe(p['height'])} — {_safe(p['image_url'])}"
        f"{_ATTRIBUTION_FOOTER}"
    )


def format_single_video(payload: dict[str, Any], response_format: str) -> str:
    if response_format == "json":
        return json.dumps({"video": video_to_json(payload)}, ensure_ascii=False)
    v = video_to_json(payload)
    return (
        f"- #{_safe(v['id'])} — {_safe(v['duration_seconds'], '?')}s "
        f"{_safe(v['width'])}x{_safe(v['height'])} — by "
        f"[{_safe(v['uploader_name'])}]({_safe(v['uploader_url'])}) — "
        f"{_safe(v['video_url'])}"
        f"{_ATTRIBUTION_FOOTER}"
    )


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
