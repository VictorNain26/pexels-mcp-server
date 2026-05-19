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


class PhotoProjection(TypedDict):
    """Minimal LLM-actionable shape for a Pexels photo."""

    id: int | None
    alt: str | None
    page_url: str | None
    photographer: str | None
    photographer_url: str | None
    width: int | None
    height: int | None
    image_url: str | None


class VideoProjection(TypedDict):
    """Minimal LLM-actionable shape for a Pexels video (top file only)."""

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
    """Diagnostic block surfaced when a post-hoc filter wiped every candidate."""

    applied_filters: dict[str, Any]
    pre_filter_count: int
    post_filter_count: int
    suggestion: str


class _SearchListBase(TypedDict):
    """Required pagination fields shared by every search/list envelope."""

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
    """``pexels_search_photos`` return envelope.

    Optional fields are typed ``T | None`` (not just ``T``) because the
    MCP SDK 1.27 dumps the result with ``model_dump(mode="json")`` without
    ``exclude_unset=True``: optional TypedDict fields with a Pydantic
    default of ``None`` end up as ``"field": null`` in
    ``structuredContent`` even when the tool never set them. The strict
    JSON-Schema generated from a non-nullable ``int`` / ``FilterDiagnostics``
    annotation rejects ``null`` (``"None is not of type 'object'"`` for
    nested TypedDicts) and the call fails with an output validation
    error. ``T | None`` produces an ``anyOf`` schema that accepts both
    branches, so the bogus ``null`` injected by the SDK validates."""

    total_results: int | None
    next_page: int | None
    filter_diagnostics: FilterDiagnostics | None


class _VideoListRequired(_SearchListBase):
    videos: list[VideoProjection]


class VideoListResult(_VideoListRequired, total=False):
    """``pexels_search_videos`` return envelope. See :class:`PhotoListResult`
    for why optional fields are explicitly ``T | None``."""

    total_results: int | None
    next_page: int | None
    filter_diagnostics: FilterDiagnostics | None


class _CollectionMediaRequired(_SearchListBase):
    id: str | None
    photos: list[PhotoProjection]
    videos: list[VideoProjection]


class CollectionMediaResult(_CollectionMediaRequired, total=False):
    """``pexels_get_collection_media`` return envelope. ``photos`` and
    ``videos`` are always present (possibly empty) so the agent can
    iterate both lists unconditionally. See :class:`PhotoListResult` for
    why optional fields are explicitly ``T | None``."""

    total_results: int | None
    next_page: int | None
    filter_diagnostics: FilterDiagnostics | None


class SinglePhotoResult(TypedDict):
    """``pexels_get_photo`` return envelope."""

    photo: PhotoProjection


class SingleVideoResult(TypedDict):
    """``pexels_get_video`` return envelope."""

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


def format_photo_list(payload: dict[str, Any]) -> PhotoListResult:
    return cast(
        PhotoListResult,
        _envelope(
            payload,
            items_key="photos",
            items=payload.get("photos") or [],
            media_projector=photo_to_json,
        ),
    )


def format_video_list(payload: dict[str, Any]) -> VideoListResult:
    return cast(
        VideoListResult,
        _envelope(
            payload,
            items_key="videos",
            items=payload.get("videos") or [],
            media_projector=video_to_json,
        ),
    )


def format_collection_media(payload: dict[str, Any]) -> CollectionMediaResult:
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
    return cast(CollectionMediaResult, out)


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
