"""FastMCP server exposing the eight Pexels tools.

Every tool is read-only against the Pexels public REST API. Inputs are validated
by a Pydantic model from ``schemas.py`` (``ConfigDict(extra="forbid")``) before
the HTTP call. Outputs are either Markdown (default) or JSON, controlled by the
``response_format`` argument.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from .client import (
    PexelsAPIError,
    PexelsAuthError,
    PexelsClient,
    PexelsRateLimitError,
)
from .formatters import (
    format_collection_list,
    format_collection_media,
    format_photo_list,
    format_single_photo,
    format_single_video,
    format_video_list,
)
from .schemas import (
    CollectionMediaParams,
    CollectionMediaType,
    CuratedPhotosParams,
    FeaturedCollectionsParams,
    GetPhotoParams,
    GetVideoParams,
    Orientation,
    PhotoSize,
    PopularVideosParams,
    ResponseFormat,
    SearchPhotosParams,
    SearchVideosParams,
    SortOrder,
    VideoSize,
)

logger = logging.getLogger("pexels_mcp_server.server")


@dataclass
class AppContext:
    """Shared lifespan context. Holds the single ``PexelsClient`` instance."""

    client: PexelsClient


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    """Boot a single ``PexelsClient`` for the server lifetime.

    Missing-key validation lives in ``PexelsClient.__init__`` and in the CLI
    entrypoint (``__main__.main``) so the FastMCP task group never has to
    surface a wrapped ``PexelsAuthError``.
    """
    client = PexelsClient(api_key=os.environ.get("PEXELS_API_KEY", ""))
    logger.info("Pexels client ready (transport managed by FastMCP).")
    try:
        yield AppContext(client=client)
    finally:
        await client.aclose()
        logger.info("Pexels client closed.")


mcp: FastMCP = FastMCP(name="pexels-mcp-server", lifespan=_lifespan)


_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _client(ctx: Context) -> PexelsClient:  # type: ignore[type-arg]
    """Pull the lifespan-scoped Pexels client out of the request context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


def _format_error(exc: Exception) -> str:
    """Render an exception as a tool-facing error string."""
    if isinstance(exc, ValidationError):
        return "Invalid parameters: " + "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
    if isinstance(exc, PexelsAuthError | PexelsRateLimitError | PexelsAPIError):
        return f"Error: {exc}"
    return f"Unexpected error: {exc.__class__.__name__}: {exc}"


@mcp.tool(
    name="pexels_search_photos",
    title="Search Pexels Photos",
    annotations=ToolAnnotations(
        title="Search Pexels Photos",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def pexels_search_photos(
    ctx: Context,  # type: ignore[type-arg]
    query: str,
    orientation: Orientation | None = None,
    size: PhotoSize | None = None,
    color: str | None = None,
    locale: str | None = None,
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """Search Pexels for free stock photos matching a query.

    Use when the user asks for free, royalty-free, attribution-friendly stock
    photos that can be used commercially.

    Do NOT use for: photos requiring a model release, exclusive stock photos,
    AI image generation, or anything that needs user-uploaded photos.

    Parameters:
    - query: search string. Required. 1-200 characters.
    - orientation: landscape | portrait | square.
    - size: large (>=24MP) | medium (>=12MP) | small (>=4MP).
    - color: one of red, orange, yellow, green, turquoise, blue, violet, pink,
      brown, black, gray, white OR a 6-digit hex without leading "#".
    - locale: BCP-47 locale, e.g. en-US, fr-FR.
    - page: 1-based page index.
    - per_page: 1-80, default 15.
    - response_format: "markdown" (default) or "json".

    Returns: a list of photos with photographer attribution. The JSON envelope
    includes total_results, has_more, next_page and rate_limit info.

    Rate limit: Pexels caps at 200 req/h and 20,000 req/month per key. Tool will
    surface the remaining quota in the rate_limit envelope.

    Errors are returned as an "Error: ..." string with actionable guidance.
    """
    try:
        params = SearchPhotosParams(
            query=query,
            orientation=orientation,
            size=size,
            color=color,
            locale=locale,
            page=page,
            per_page=per_page,
            response_format=response_format,
        )
        payload, rate_limit = await _client(ctx).search_photos(
            query=params.query,
            orientation=params.orientation.value if params.orientation else None,
            size=params.size.value if params.size else None,
            color=params.color,
            locale=params.locale,
            page=params.page,
            per_page=params.per_page,
        )
        return format_photo_list(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_curated_photos",
    title="Get Curated Pexels Photos",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_curated_photos(
    ctx: Context,  # type: ignore[type-arg]
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """Return Pexels' editorial-curated photos. No query needed.

    Use when the user wants tasteful, hand-picked stock imagery and is not
    looking for a specific subject.

    Do NOT use when the user has a search term: call ``pexels_search_photos``
    instead.

    Parameters:
    - page: 1-based page index.
    - per_page: 1-80, default 15.
    - response_format: "markdown" (default) or "json".

    Returns: same envelope as ``pexels_search_photos`` (photos list + rate
    limit).
    """
    try:
        params = CuratedPhotosParams(
            page=page,
            per_page=per_page,
            response_format=response_format,
        )
        payload, rate_limit = await _client(ctx).curated_photos(
            page=params.page,
            per_page=params.per_page,
        )
        return format_photo_list(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_get_photo",
    title="Get a Pexels Photo",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_get_photo(
    ctx: Context,  # type: ignore[type-arg]
    photo_id: int,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """Fetch a single Pexels photo by its numeric id.

    Use when you already have a photo id (from a previous search result, a
    Pexels page URL, or the user pasted one) and need the full details.

    Do NOT use to look up a photo by description: search first.

    Parameters:
    - photo_id: positive integer photo id.
    - response_format: "markdown" (default) or "json".
    """
    try:
        params = GetPhotoParams(photo_id=photo_id, response_format=response_format)
        payload, rate_limit = await _client(ctx).get_photo(params.photo_id)
        return format_single_photo(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_search_videos",
    title="Search Pexels Videos",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_search_videos(
    ctx: Context,  # type: ignore[type-arg]
    query: str,
    orientation: Orientation | None = None,
    size: VideoSize | None = None,
    locale: str | None = None,
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """Search Pexels for free stock videos matching a query.

    Use when the user asks for royalty-free stock B-roll or video clips.

    Do NOT use for: TV/movie clips, copyrighted content, or video generation.

    Parameters:
    - query: search string. Required. 1-200 characters.
    - orientation: landscape | portrait | square.
    - size: large (4K) | medium (Full HD) | small (HD).
    - locale: BCP-47 locale.
    - page: 1-based page index.
    - per_page: 1-80, default 15.
    - response_format: "markdown" (default) or "json".

    Returns: an envelope with the videos list, pagination flags and rate
    limit.
    """
    try:
        params = SearchVideosParams(
            query=query,
            orientation=orientation,
            size=size,
            locale=locale,
            page=page,
            per_page=per_page,
            response_format=response_format,
        )
        payload, rate_limit = await _client(ctx).search_videos(
            query=params.query,
            orientation=params.orientation.value if params.orientation else None,
            size=params.size.value if params.size else None,
            locale=params.locale,
            page=params.page,
            per_page=params.per_page,
        )
        return format_video_list(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_popular_videos",
    title="Get Popular Pexels Videos",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_popular_videos(
    ctx: Context,  # type: ignore[type-arg]
    min_width: int | None = None,
    min_height: int | None = None,
    min_duration: int | None = None,
    max_duration: int | None = None,
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """Return Pexels' currently popular videos, optionally filtered by size or
    duration.

    Use when the user wants trending B-roll without a specific topic.

    Do NOT use when the user has a search term: call ``pexels_search_videos``.

    Parameters:
    - min_width / min_height: minimum pixel dimensions.
    - min_duration / max_duration: bounds in seconds.
    - page: 1-based page index.
    - per_page: 1-80, default 15.
    - response_format: "markdown" (default) or "json".
    """
    try:
        params = PopularVideosParams(
            min_width=min_width,
            min_height=min_height,
            min_duration=min_duration,
            max_duration=max_duration,
            page=page,
            per_page=per_page,
            response_format=response_format,
        )
        payload, rate_limit = await _client(ctx).popular_videos(
            min_width=params.min_width,
            min_height=params.min_height,
            min_duration=params.min_duration,
            max_duration=params.max_duration,
            page=params.page,
            per_page=params.per_page,
        )
        return format_video_list(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_get_video",
    title="Get a Pexels Video",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_get_video(
    ctx: Context,  # type: ignore[type-arg]
    video_id: int,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """Fetch a single Pexels video by its numeric id.

    Use when you already have a video id and need the full asset details
    (video_files, qualities, uploader, etc.).

    Parameters:
    - video_id: positive integer video id.
    - response_format: "markdown" (default) or "json".
    """
    try:
        params = GetVideoParams(video_id=video_id, response_format=response_format)
        payload, rate_limit = await _client(ctx).get_video(params.video_id)
        return format_single_video(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_list_featured_collections",
    title="List Featured Pexels Collections",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_list_featured_collections(
    ctx: Context,  # type: ignore[type-arg]
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """List Pexels editor-featured collections (themed bundles of photos and
    videos).

    Use when the user wants to browse curated themes (e.g. "abstract", "summer
    work-from-home").

    Parameters:
    - page: 1-based page index.
    - per_page: 1-80, default 15.
    - response_format: "markdown" (default) or "json".
    """
    try:
        params = FeaturedCollectionsParams(
            page=page,
            per_page=per_page,
            response_format=response_format,
        )
        payload, rate_limit = await _client(ctx).list_featured_collections(
            page=params.page,
            per_page=params.per_page,
        )
        return format_collection_list(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_get_collection_media",
    title="Get Pexels Collection Media",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_get_collection_media(
    ctx: Context,  # type: ignore[type-arg]
    collection_id: str,
    type: CollectionMediaType | None = None,
    sort: SortOrder | None = None,
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """List the photos and videos inside a Pexels collection.

    Use after ``pexels_list_featured_collections`` to drill into a specific
    collection by id.

    Parameters:
    - collection_id: id from the featured collections list. Required.
    - type: "photos" or "videos" to filter. Defaults to both.
    - sort: "asc" or "desc" by creation date.
    - page: 1-based page index.
    - per_page: 1-80, default 15.
    - response_format: "markdown" (default) or "json".
    """
    try:
        params = CollectionMediaParams(
            collection_id=collection_id,
            type=type,
            sort=sort,
            page=page,
            per_page=per_page,
            response_format=response_format,
        )
        payload, rate_limit = await _client(ctx).get_collection_media(
            collection_id=params.collection_id,
            type=params.type.value if params.type else None,
            sort=params.sort.value if params.sort else None,
            page=params.page,
            per_page=params.per_page,
        )
        return format_collection_media(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)
