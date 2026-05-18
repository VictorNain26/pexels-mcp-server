"""FastMCP server exposing the Pexels API to MCP-aware AI agents.

Every tool is read-only. Outputs default to a JSON envelope shaped for
direct consumption by an agent (parseable, no per-resolution clutter). All
responses include a ``rate_limit`` block so the agent can pace itself.

Pexels free tier: 200 requests/hour, 20 000 requests/month. The server
logs a warning to stderr when fewer than 100 requests remain.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings
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
from .previews import fetch_thumbnails
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
    PreviewMediaParams,
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

    The client never stores a Pexels key. The effective key is resolved per
    tool call via ``_resolve_api_key`` (HTTP header first, env var fallback).
    """
    client = PexelsClient()
    logger.info("Pexels client ready (transport managed by FastMCP).")
    try:
        yield AppContext(client=client)
    finally:
        await client.aclose()
        logger.info("Pexels client closed.")


def _resolve_api_key(ctx: Context) -> str | None:  # type: ignore[type-arg]
    """Resolve the Pexels API key for the current call.

    Order of precedence:

    1. The ``X-Pexels-Api-Key`` header on the live HTTP request (read straight
       from the Starlette ``Request`` exposed by FastMCP). This is the
       canonical source in HTTP / Streamable-HTTP mode, because FastMCP spawns
       its session worker at initialize time and would freeze any ContextVar
       set later by ASGI middleware.
    2. The ``pexels_key_ctx`` ContextVar populated by ``pexels_key_middleware``
       in ``stateless_http`` deployments (each request runs in its own task,
       so the var propagates).
    3. The ``PEXELS_API_KEY`` env var (stdio transport, local config).
    """
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    if request is not None:
        header_val = ""
        headers = getattr(request, "headers", None)
        if headers is not None:
            header_val = str(headers.get("x-pexels-api-key", "")).strip()
        if header_val:
            return header_val

    # Lazy import keeps stdio boot light.
    from .transport import pexels_key_ctx

    cv_val = pexels_key_ctx.get()
    if cv_val:
        return cv_val

    return os.environ.get("PEXELS_API_KEY", "").strip() or None


def _build_transport_security() -> TransportSecuritySettings:
    """Configure DNS rebinding protection for the HTTP transport.

    By default FastMCP enables a strict ``allowed_hosts`` whitelist limited to
    localhost, which is correct for laptop dev but breaks every public
    deployment (the Host header is the platform's domain). The Bearer auth
    middleware already covers the threat that DNS rebinding aims at, so we
    flip the default and let operators opt back in via ``MCP_ALLOWED_HOSTS``
    (comma-separated, supports the ``host:*`` wildcard the SDK accepts).
    """
    allowed_hosts_env = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    if not allowed_hosts_env:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h.strip() for h in allowed_hosts_env.split(",") if h.strip()],
    )


mcp: FastMCP = FastMCP(
    name="pexels-mcp-server",
    lifespan=_lifespan,
    transport_security=_build_transport_security(),
)


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
    """Render an exception as a tool-facing error string.

    Validation errors list the offending field with the constraint that failed,
    so the agent can retry with corrected arguments. Pexels errors carry their
    own actionable text (set PEXELS_API_KEY, reduce request frequency, ...).
    """
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
    response_format: ResponseFormat = ResponseFormat.JSON,
) -> str:
    """Search Pexels for free, commercially usable stock photos.

    USE WHEN: the user needs an illustration for an article, slide,
      newsletter, blog post or any UI mockup and gives a topic in plain
      language. Examples: "mountain landscape at sunrise", "people working
      from home", "blue abstract texture".
    DO NOT USE WHEN: the user wants AI-generated images (this tool only
      returns existing Pexels assets), images of specific real people, or
      copyrighted material like film stills or product packaging.

    Returns a JSON envelope: {total_results, page, per_page, count,
    has_more, next_page, rate_limit, photos:[{id, alt, page_url,
    photographer, photographer_url, width, height, image_url,
    thumbnail_url}]}. Use ``image_url`` for embedding at full resolution
    and ``thumbnail_url`` for previews. Cite ``photographer`` and
    ``photographer_url`` when publishing.

    Filters narrow results aggressively. ``color`` accepts the 12 named
    colors (red, orange, yellow, green, turquoise, blue, violet, pink,
    brown, black, gray, white) or a 6-digit hex without '#'. Start with no
    filter; add filters only if the first page is off-target.

    ``per_page`` is capped at 80 by Pexels. Default 15 keeps the response
    under ~3 KB. Paginate via ``page`` rather than raising ``per_page`` if
    the user wants more.
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
            api_key=_resolve_api_key(ctx),
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
    title="Browse Curated Pexels Photos",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_curated_photos(
    ctx: Context,  # type: ignore[type-arg]
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.JSON,
) -> str:
    """Browse Pexels' editor-curated photo feed (no search query).

    USE WHEN: the user wants inspiration or "anything tasteful" without a
      topic in mind. Example: "give me a few hero images we could try".
    DO NOT USE WHEN: the user named a subject. Use ``pexels_search_photos``
      so results actually match what they asked for.

    Returns the same envelope as ``pexels_search_photos``. Curated feed
    updates daily, so re-querying ``page=1`` after a day yields new photos.
    """
    try:
        params = CuratedPhotosParams(
            page=page,
            per_page=per_page,
            response_format=response_format,
        )
        payload, rate_limit = await _client(ctx).curated_photos(
            api_key=_resolve_api_key(ctx),
            page=params.page,
            per_page=params.per_page,
        )
        return format_photo_list(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_get_photo",
    title="Get a Pexels Photo by ID",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_get_photo(
    ctx: Context,  # type: ignore[type-arg]
    photo_id: int,
    response_format: ResponseFormat = ResponseFormat.JSON,
) -> str:
    """Fetch a single Pexels photo by its numeric id.

    USE WHEN: a previous search or a Pexels URL gave you a photo id and you
      want the canonical record (alt text, dimensions, author, full-res
      URL). Example: id=28448939 from a search hit, or extracted from
      "pexels.com/photo/foo-28448939".
    DO NOT USE WHEN: the user describes the photo in words. Call
      ``pexels_search_photos`` first.

    Returns the same per-photo shape as the search envelope, wrapped in
    ``{photo: {...}, rate_limit: {...}}``.
    """
    try:
        params = GetPhotoParams(photo_id=photo_id, response_format=response_format)
        payload, rate_limit = await _client(ctx).get_photo(
            params.photo_id, api_key=_resolve_api_key(ctx)
        )
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
    response_format: ResponseFormat = ResponseFormat.JSON,
) -> str:
    """Search Pexels for free, commercially usable stock videos.

    USE WHEN: the user needs B-roll, a hero loop, an animated background,
      or video filler. Examples: "drone shot of a city skyline at dusk",
      "macro of coffee being poured", "people walking in slow motion".
    DO NOT USE WHEN: the user wants AI-generated video, copyrighted clips,
      or social media footage of specific people.

    Returns a JSON envelope: {total_results, page, per_page, count,
    has_more, next_page, rate_limit, videos:[{id, page_url,
    duration_seconds, width, height, preview_image_url, uploader_name,
    uploader_url, files:[{quality, width, height, fps, url}],
    total_files_available}]}. Each video lists only the top 3 files by
    resolution. Use ``files[0].url`` for the highest quality stream.

    ``size`` buckets: large = 4K, medium = Full HD, small = HD. Start
    without filters; the search engine ranks relevance and quality first.
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
            api_key=_resolve_api_key(ctx),
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
    title="Browse Popular Pexels Videos",
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
    response_format: ResponseFormat = ResponseFormat.JSON,
) -> str:
    """Browse Pexels' currently trending videos (no search query).

    USE WHEN: the user wants trending B-roll without a topic, or asks for
      videos of at least a given resolution / duration. Examples: "show me
      some trending clips longer than 30 seconds", "popular 4K loops".
    DO NOT USE WHEN: the user has a topic. Call ``pexels_search_videos``.

    Same envelope as ``pexels_search_videos``. Combine duration bounds
    (``min_duration``, ``max_duration`` in seconds) to find clips of a
    target length without scanning hundreds of results.
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
            api_key=_resolve_api_key(ctx),
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
    title="Get a Pexels Video by ID",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_get_video(
    ctx: Context,  # type: ignore[type-arg]
    video_id: int,
    response_format: ResponseFormat = ResponseFormat.JSON,
) -> str:
    """Fetch a single Pexels video by its numeric id.

    USE WHEN: a previous search or a Pexels URL gave you a video id and you
      want the canonical record (duration, resolution, downloadable file
      URLs, uploader credit).
    DO NOT USE WHEN: the user describes the video in words. Call
      ``pexels_search_videos`` first.

    Returns ``{video: {...same shape as the videos[] entries...},
    rate_limit: {...}}``.
    """
    try:
        params = GetVideoParams(video_id=video_id, response_format=response_format)
        payload, rate_limit = await _client(ctx).get_video(
            params.video_id, api_key=_resolve_api_key(ctx)
        )
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
    response_format: ResponseFormat = ResponseFormat.JSON,
) -> str:
    """List Pexels-curated themed collections (mixed photo + video bundles).

    USE WHEN: the user asks for a moodboard around a theme that does not
      map cleanly to a single keyword. Example: "give me a collection for
      a Scandinavian minimalist vibe".
    DO NOT USE WHEN: the user has a concrete query. Search is more direct.

    Returns ``{collections: [{id, title, description, media_count,
    photos_count, videos_count}], ...}``. Feed the ``id`` of any collection
    into ``pexels_get_collection_media`` to fetch its contents.
    """
    try:
        params = FeaturedCollectionsParams(
            page=page,
            per_page=per_page,
            response_format=response_format,
        )
        payload, rate_limit = await _client(ctx).list_featured_collections(
            api_key=_resolve_api_key(ctx),
            page=params.page,
            per_page=params.per_page,
        )
        return format_collection_list(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_get_collection_media",
    title="Get Pexels Collection Contents",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_get_collection_media(
    ctx: Context,  # type: ignore[type-arg]
    collection_id: str,
    type: CollectionMediaType | None = None,
    sort: SortOrder | None = None,
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.JSON,
) -> str:
    """Read the photos and videos inside a Pexels collection.

    USE WHEN: ``pexels_list_featured_collections`` gave you an id and you
      want its contents. Optionally pass ``type='photos'`` or
      ``type='videos'`` to filter.
    DO NOT USE WHEN: you do not have a collection id. List collections first.

    Returns ``{id, total_results, page, per_page, count, has_more,
    next_page, rate_limit, photos:[...], videos:[...]}`` with each list
    using the same per-item shape as the search tools.
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
            api_key=_resolve_api_key(ctx),
            collection_id=params.collection_id,
            type=params.type.value if params.type else None,
            sort=params.sort.value if params.sort else None,
            page=params.page,
            per_page=params.per_page,
        )
        return format_collection_media(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_preview_media",
    title="Preview Pexels Thumbnails Visually",
    annotations=_READ_ONLY_ANNOTATIONS,
    structured_output=False,
)
async def pexels_preview_media(
    ctx: Context,  # type: ignore[type-arg]
    thumbnail_urls: list[str],
) -> Any:
    """Fetch small Pexels thumbnails and return them as inline images.

    USE WHEN: you ran a search, you have 2-6 candidates, and you need to
      see them to pick the best fit. Pass the ``thumbnail_url`` field of
      photos or the ``preview_image_url`` field of videos that the search
      tools already returned.
    DO NOT USE WHEN: you have not searched yet. Search first; this tool
      does not look anything up. Also skip this tool if the client model
      cannot consume image content (text-only models). For pure text
      picking, the ``alt`` field on each photo is already in the search
      envelope and is enough most of the time.

    Returns one TextContent block summarizing the batch followed by one
    ImageContent block per URL (in the same order). Each thumbnail is
    base64-encoded inline; the calling model decides which id to use.

    Only URLs whose host is ``images.pexels.com`` are accepted. Anything
    else is rejected at validation time. Maximum 6 URLs per call. Each
    thumbnail body is capped at 256 KB; oversized files come back as an
    error string instead of an image, with the rest of the batch
    untouched.
    """
    try:
        params = PreviewMediaParams(thumbnail_urls=thumbnail_urls)
        results = await fetch_thumbnails(params.thumbnail_urls)
    except Exception as exc:
        return [_format_error(exc)]

    blocks: list[str | Image] = []
    ok = sum(1 for r in results if r.image is not None)
    summary_lines = [f"Loaded {ok}/{len(results)} thumbnails:"]
    for index, result in enumerate(results, start=1):
        if result.image is not None:
            summary_lines.append(f"  {index}. {result.url} - ok")
        else:
            summary_lines.append(f"  {index}. {result.url} - failed: {result.error}")
    blocks.append("\n".join(summary_lines))
    for result in results:
        if result.image is not None:
            blocks.append(result.image)
    return blocks
