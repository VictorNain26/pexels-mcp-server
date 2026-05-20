"""JSON projections + typed envelopes for Pexels REST payloads.

Every tool returns a ``TypedDict`` so FastMCP can compute a real
``outputSchema`` (concrete fields, not just ``{type: object}``). The SDK
populates both ``structuredContent`` (machine-readable, validated against
the schema) and a TextContent block with the serialized JSON (backwards
compat for clients that don't yet read structured output).

The shape stays deliberately minimal: every field exposed has a clear
purpose for the LLM (alt for filtering, image_url for the download link
the agent hands back to the user, photographer + photographer_url for the
attribution line Pexels licence requires). No thumbnail variants, no
rate-limit chrome, no narrative captions.
"""

from __future__ import annotations

from typing import Any, TypedDict, cast

# Docstrings deliberately omitted from the TypedDicts below: each one
# would surface as ``description: ...`` in every tool's outputSchema
# $defs (PhotoProjection is referenced by 3 tools, VideoProjection by 3
# tools, FilterDiagnostics by 4 — so a one-liner docstring becomes 4-8
# duplicated chars in list_tools). The shape is self-documenting via
# field names; the dev-facing intent lives in the module docstring.


class PhotoProjection(TypedDict):
    id: int | None
    alt: str | None
    page_url: str | None
    photographer: str | None
    photographer_url: str | None
    width: int | None
    height: int | None
    image_url: str | None


class VideoProjection(TypedDict):
    id: int | None
    page_url: str | None
    duration_seconds: int | None
    width: int | None
    height: int | None
    uploader_name: str | None
    uploader_url: str | None
    video_url: str | None
    quality: str | None


class FilterDiagnostics(TypedDict):
    applied_filters: dict[str, Any]
    pre_filter_count: int
    post_filter_count: int
    suggestion: str


class _SearchListBase(TypedDict):
    # Required pagination fields shared by every search/list envelope.
    page: int
    per_page: int
    count: int
    has_more: bool


# Per-tool intermediate bases carry the always-present ``items_key`` field
# as ``Required``. Python 3.10 lacks ``typing.Required`` so we use the
# subclass pattern: required keys are declared in a parent TypedDict with
# ``total=True``, optional keys in a child with ``total=False``. Clients
# reading the generated ``outputSchema`` see both ``photos``/``videos``
# and the pagination fields under ``required``.


class _PhotoListRequired(_SearchListBase):
    photos: list[PhotoProjection]


class PhotoListResult(_PhotoListRequired, total=False):
    total_results: int
    next_page: int
    filter_diagnostics: FilterDiagnostics


class _VideoListRequired(_SearchListBase):
    videos: list[VideoProjection]


class VideoListResult(_VideoListRequired, total=False):
    total_results: int
    next_page: int
    filter_diagnostics: FilterDiagnostics


class _CollectionMediaRequired(_SearchListBase):
    id: str | None
    photos: list[PhotoProjection]
    videos: list[VideoProjection]


class CollectionMediaResult(_CollectionMediaRequired, total=False):
    # ``photos`` + ``videos`` are always present (possibly empty) so the
    # agent can iterate both lists unconditionally regardless of ``type``.
    total_results: int
    next_page: int
    filter_diagnostics: FilterDiagnostics


class SinglePhotoResult(TypedDict):
    photo: PhotoProjection


class SingleVideoResult(TypedDict):
    video: VideoProjection


def photo_to_json(photo: dict[str, Any]) -> PhotoProjection:
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


def video_to_json(video: dict[str, Any]) -> VideoProjection:
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


def _pagination_block(payload: dict[str, Any], count: int) -> dict[str, Any]:
    """Pagination fields shared by every list / search / collection envelope.

    Optional ``total_results`` and ``next_page`` are emitted only when the
    upstream payload carries them; ``filter_diagnostics`` only when the
    tool layer attached one (post-hoc filter wiped the page).
    """
    page = int(payload.get("page", 1))
    out: dict[str, Any] = {
        "page": page,
        "per_page": int(payload.get("per_page", count)),
        "count": count,
        "has_more": bool(payload.get("next_page")),
    }
    if (total := payload.get("total_results")) is not None:
        out["total_results"] = total
    if out["has_more"]:
        out["next_page"] = page + 1
    if diagnostics := payload.get("filter_diagnostics"):
        out["filter_diagnostics"] = diagnostics
    return out


def format_photo_list(payload: dict[str, Any]) -> PhotoListResult:
    items = payload.get("photos") or []
    return cast(
        PhotoListResult,
        {
            **_pagination_block(payload, len(items)),
            "photos": [photo_to_json(p) for p in items],
        },
    )


def format_video_list(payload: dict[str, Any]) -> VideoListResult:
    items = payload.get("videos") or []
    return cast(
        VideoListResult,
        {
            **_pagination_block(payload, len(items)),
            "videos": [video_to_json(v) for v in items],
        },
    )


def format_collection_media(payload: dict[str, Any]) -> CollectionMediaResult:
    media = payload.get("media") or []
    return cast(
        CollectionMediaResult,
        {
            "id": payload.get("id"),
            **_pagination_block(payload, len(media)),
            "photos": [photo_to_json(m) for m in media if m.get("type") == "Photo"],
            "videos": [video_to_json(m) for m in media if m.get("type") == "Video"],
        },
    )


def format_single_photo(payload: dict[str, Any]) -> SinglePhotoResult:
    return {"photo": photo_to_json(payload)}


def format_single_video(payload: dict[str, Any]) -> SingleVideoResult:
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
