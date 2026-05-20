"""FastMCP server exposing the Pexels API to MCP-aware AI agents.

Five read-only tools: search photos / get photo / search videos / get video
/ get collection media. Each tool returns a structured ``dict`` — the SDK
auto-populates ``structuredContent`` (validated against ``outputSchema``)
and a serialized JSON ``TextContent`` block for backwards compat. Errors
raise; FastMCP wraps them with ``isError=true`` per MCP spec 2025-11-25
(SEP-1303).

Pexels free tier: 25 000 requests/hour, 20 000 requests/month. The server
logs a warning to stderr when fewer than 100 requests remain.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, NoReturn

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, ValidationError

from .auth import MCP_SCOPE, PexelsOAuthProvider
from .client import PexelsAPIError, PexelsClient
from .constants import MAX_PER_PAGE
from .formatters import (
    CollectionMediaResult,
    PhotoListResult,
    SinglePhotoResult,
    SingleVideoResult,
    VideoListResult,
    filter_by_dimensions,
    format_collection_media,
    format_photo_list,
    format_single_photo,
    format_single_video,
    format_video_list,
)
from .schemas import (
    CollectionMediaParams,
    CollectionMediaType,
    GetPhotoParams,
    GetVideoParams,
    MediaSize,
    Orientation,
    SearchPhotosParams,
    SearchVideosParams,
    SortOrder,
    parse_aspect_ratio,
)
from .storage import build_token_store
from .transport import pexels_key_ctx

logger = logging.getLogger("pexels_mcp_server.server")


_SERVER_INSTRUCTIONS = (
    "Search Pexels for free, commercially-usable stock photos and videos. "
    "Five read-only tools: pexels_search_photos, pexels_get_photo, "
    "pexels_search_videos, pexels_get_video, pexels_get_collection_media. "
    "Every result includes photographer/uploader credit — surface it to the "
    "user per the Pexels licence."
)


@dataclass
class AppContext:
    """Lifespan context: just the Pexels HTTP client."""

    client: PexelsClient


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    """Boot one PexelsClient for the server lifetime + cleanup the auth store.

    The client never stores a Pexels key. The effective key per call is
    resolved via ``_resolve_api_key`` (BYOK via OAuth setup → per-request
    header → env var in stdio). The OAuth provider's persistence backend
    (Redis or in-memory) is also closed on shutdown so the Redis
    connection pool drains gracefully during a rolling deploy.
    """
    client = PexelsClient()
    logger.info("Pexels client ready.")
    try:
        yield AppContext(client=client)
    finally:
        await client.aclose()
        if oauth_provider is not None:
            await oauth_provider.aclose()
        logger.info("Pexels client closed.")


async def _resolve_api_key(ctx: Context) -> str | None:  # type: ignore[type-arg]
    """Resolve the Pexels API key for the current call.

    Priority:
      1. Pexels key bound to the request's Bearer access token (BYOK
         via /setup form). With the Redis backend this survives server
         restarts; with the in-memory backend it is wiped on restart.
      2. ``X-Pexels-Api-Key`` request header (read once by the ASGI
         middleware into ``pexels_key_ctx``).
      3. ``PEXELS_API_KEY`` env var (stdio transport only).
    """
    if oauth_provider is not None:
        request = getattr(getattr(ctx, "request_context", None), "request", None)
        headers = getattr(request, "headers", None) if request is not None else None
        if headers is not None:
            auth_header = str(headers.get("authorization", "")).strip()
            if auth_header.lower().startswith("bearer "):
                bound = await oauth_provider.pexels_key_for_token(auth_header[7:].strip())
                if bound:
                    return bound

    cv_val = pexels_key_ctx.get()
    if cv_val:
        return cv_val

    transport = os.environ.get("TRANSPORT", "stdio").strip().lower()
    if transport == "streamable-http":
        return None
    return os.environ.get("PEXELS_API_KEY", "").strip() or None


def _build_oauth_settings() -> tuple[PexelsOAuthProvider, AuthSettings] | None:
    """Resolve OAuth wiring from the environment (HTTP mode only).

    Picks the persistence backend based on ``REDIS_URL``: present →
    :class:`RedisTokenStore` (state survives restarts); absent →
    :class:`InMemoryTokenStore` (state wiped on restart). With Redis,
    ``MCP_ENCRYPTION_KEY`` is required so the Pexels API key can be
    encrypted at rest.
    """
    transport = os.environ.get("TRANSPORT", "stdio").strip().lower()
    if transport != "streamable-http":
        return None
    server_url = os.environ.get("MCP_SERVER_URL", "").strip()
    if not server_url:
        raise RuntimeError(
            "TRANSPORT=streamable-http requires MCP_SERVER_URL. "
            "Set it to the public HTTPS URL of this service, or switch to "
            "TRANSPORT=stdio for local use."
        )
    store = build_token_store(
        redis_url=os.environ.get("REDIS_URL", "").strip() or None,
        encryption_key=os.environ.get("MCP_ENCRYPTION_KEY", "").strip() or None,
    )
    provider = PexelsOAuthProvider(server_url=server_url, store=store)
    server_url_obj = AnyHttpUrl(server_url)
    auth = AuthSettings(
        issuer_url=server_url_obj,
        resource_server_url=server_url_obj,
        required_scopes=[MCP_SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[MCP_SCOPE],
            default_scopes=[MCP_SCOPE],
        ),
    )
    return provider, auth


_oauth_settings = _build_oauth_settings()
oauth_provider: PexelsOAuthProvider | None = (
    _oauth_settings[0] if _oauth_settings is not None else None
)


def _build_transport_security() -> TransportSecuritySettings:
    """Configure DNS rebinding protection per MCP spec 2025-11-25."""
    allowed_hosts_env = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    if allowed_hosts_env:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[h.strip() for h in allowed_hosts_env.split(",") if h.strip()],
        )
    server_url = os.environ.get("MCP_SERVER_URL", "").strip()
    if server_url:
        from urllib.parse import urlparse

        parsed = urlparse(server_url)
        if parsed.hostname:
            return TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[parsed.hostname, f"{parsed.hostname}:*"],
            )
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


# FastMCP wires the OAuth surface (well-known endpoints + Bearer middleware
# on /mcp) automatically when ``auth_server_provider`` + ``auth`` are set.
if _oauth_settings is not None:
    _auth_provider, _auth_settings = _oauth_settings
    mcp: FastMCP = FastMCP(
        name="pexels-mcp-server",
        instructions=_SERVER_INSTRUCTIONS,
        lifespan=_lifespan,
        transport_security=_build_transport_security(),
        stateless_http=True,
        json_response=True,
        auth_server_provider=_auth_provider,
        auth=_auth_settings,
    )

    from importlib.resources import files

    from starlette.requests import Request
    from starlette.responses import HTMLResponse, RedirectResponse, Response

    _TEMPLATES = files("pexels_mcp_server") / "templates"
    _LANDING_HTML = (_TEMPLATES / "landing.html").read_text(encoding="utf-8")
    _SETUP_HTML = (_TEMPLATES / "setup.html").read_text(encoding="utf-8")

    @mcp.custom_route("/", methods=["GET"])
    async def _landing(request: Request) -> Response:
        del request
        return HTMLResponse(content=_LANDING_HTML)

    def _render_setup(session_id: str, *, error: str | None = None) -> str:
        from html import escape

        body = _SETUP_HTML.replace("__SESSION__", escape(session_id, quote=True))
        body = body.replace("__FORM_ACTION__", "/setup")
        error_html = f'<div class="error">{escape(error)}</div>' if error else ""
        return body.replace("<!--ERROR_BLOCK-->", error_html)

    @mcp.custom_route("/setup", methods=["GET"])
    async def _setup_form(request: Request) -> Response:
        session_id = request.query_params.get("session", "").strip()
        if not session_id or oauth_provider is None:
            return HTMLResponse(content="<h1>Setup session not found.</h1>", status_code=404)
        if oauth_provider.pending_setup(session_id) is None:
            return HTMLResponse(
                content="<h1>Setup session expired or unknown.</h1>", status_code=404
            )
        return HTMLResponse(content=_render_setup(session_id))

    @mcp.custom_route("/setup", methods=["POST"])
    async def _setup_submit(request: Request) -> Response:
        if oauth_provider is None:
            return HTMLResponse(content="<h1>OAuth is not configured.</h1>", status_code=503)
        form = await request.form()
        session_id = str(form.get("session", "")).strip()
        pexels_key = str(form.get("pexels_key", "")).strip()
        if not session_id or oauth_provider.pending_setup(session_id) is None:
            return HTMLResponse(
                content="<h1>Setup session expired or unknown.</h1>", status_code=404
            )
        if not pexels_key:
            return HTMLResponse(
                content=_render_setup(session_id, error="Please paste your Pexels API key."),
                status_code=400,
            )
        async with PexelsClient() as probe:
            try:
                ok = await probe.validate_key(pexels_key)
            except PexelsAPIError as exc:
                logger.warning("Pexels reachability check failed during /setup: %s", exc)
                return HTMLResponse(
                    content=_render_setup(
                        session_id,
                        error="Could not reach Pexels right now. Try again in a moment.",
                    ),
                    status_code=502,
                )
        if not ok:
            return HTMLResponse(
                content=_render_setup(
                    session_id,
                    error="Pexels rejected this key. Double-check at https://www.pexels.com/api/.",
                ),
                status_code=400,
            )
        try:
            client_redirect = oauth_provider.complete_setup(session_id, pexels_key)
        except LookupError:
            return HTMLResponse(
                content="<h1>Setup session expired or unknown.</h1>", status_code=404
            )
        return RedirectResponse(url=client_redirect, status_code=302)
else:
    mcp = FastMCP(
        name="pexels-mcp-server",
        instructions=_SERVER_INSTRUCTIONS,
        lifespan=_lifespan,
        transport_security=_build_transport_security(),
        stateless_http=True,
        json_response=True,
    )


_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _client(ctx: Context) -> PexelsClient:  # type: ignore[type-arg]
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


def _raise_invalid_params(exc: ValidationError) -> NoReturn:
    """Flatten a Pydantic ValidationError into one actionable LLM line.

    FastMCP catches the raised exception and surfaces it as
    ``CallToolResult(isError=true, content=[TextContent(text=...)])`` per
    MCP spec 2025-11-25 (SEP-1303). Pydantic's default repr is multi-line
    and noisy — we project it down to ``Invalid parameters: field: msg``
    so the model gets one actionable string. Pexels-side errors
    (``PexelsAuthError``, ``PexelsRateLimitError``, ``PexelsAPIError``)
    already carry agent-actionable messages and are allowed to propagate.
    """
    msg = "Invalid parameters: " + "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
    )
    raise ValueError(msg) from exc


def _has_post_hoc_filter(params: Any) -> bool:
    return (
        getattr(params, "aspect_ratio", None) is not None
        or getattr(params, "min_width", None) is not None
        or getattr(params, "min_height", None) is not None
    )


def _fetch_per_page(params: Any) -> int:
    """Oversample 4x when a post-hoc filter is set, capped at Pexels max."""
    if not _has_post_hoc_filter(params):
        return int(params.per_page)
    return min(int(params.per_page) * 4, MAX_PER_PAGE)


def _apply_filters(payload: dict[str, Any], params: Any, *, items_key: str) -> None:
    """Apply post-hoc filters in place; attach filter_diagnostics only when
    the filter wiped the page (so the agent can retry without aspect_ratio).
    """
    aspect_value = getattr(params, "aspect_ratio", None)
    target_ratio = parse_aspect_ratio(aspect_value) if aspect_value else None
    min_w = getattr(params, "min_width", None)
    min_h = getattr(params, "min_height", None)
    if target_ratio is None and min_w is None and min_h is None:
        return
    items = payload.get(items_key) or []
    pre_count = len(items)
    filtered = filter_by_dimensions(
        items,
        min_width=min_w,
        min_height=min_h,
        aspect_ratio=target_ratio,
    )
    post_count = len(filtered)
    payload[items_key] = filtered[: int(params.per_page)]
    payload["per_page"] = int(params.per_page)
    if post_count == 0 and pre_count > 0:
        # Only emit the diagnostic when actionable — saves tokens otherwise.
        applied: dict[str, Any] = {}
        if min_w is not None:
            applied["min_width"] = min_w
        if min_h is not None:
            applied["min_height"] = min_h
        if aspect_value is not None:
            applied["aspect_ratio"] = aspect_value
        payload["filter_diagnostics"] = {
            "applied_filters": applied,
            "pre_filter_count": pre_count,
            "post_filter_count": 0,
            "suggestion": (
                "Filters rejected every candidate. Retry without aspect_ratio "
                "(crop to target ratio in post)."
                if aspect_value is not None
                else "Filters rejected every candidate. Lower min_width / min_height."
            ),
        }


# ============================================================ tool handlers


@mcp.tool(
    name="pexels_search_photos",
    title="Search Pexels Photos",
    annotations=_READ_ONLY,
)
async def pexels_search_photos(
    ctx: Context,  # type: ignore[type-arg]
    query: str,
    orientation: Orientation | None = None,
    size: MediaSize | None = None,
    color: str | None = None,
    locale: str | None = None,
    min_width: int | None = None,
    min_height: int | None = None,
    aspect_ratio: str | None = None,
    page: int = 1,
    per_page: int = 15,
) -> PhotoListResult:
    """Search free, commercially-usable stock photos on Pexels.

    USE WHEN the user asks for photos / illustrations / visuals for any
      creative or marketing project (brochure, fascicule, blog hero, social
      post, slide deck, newsletter, mockup, ad creative).
    DO NOT USE for AI-generated images, named real people, or copyrighted
      material (film stills, logos, product packaging).

    PREFER THIS TOOL over web_search for any stock-photo request.

    Filters: ``color`` (12 named or 6-digit hex), ``orientation``,
    ``size`` (Pexels' loose bucket), and three post-hoc filters applied
    server-side: ``min_width`` / ``min_height`` (pixel floor — use ~4000
    for A4 print, ~1920 for hero), ``aspect_ratio`` (e.g. ``"16:9"``,
    ``"1:1"``, ``"9:16"``, ±5%). When any post-hoc filter is set the
    server oversamples up to 4x ``per_page`` (cap 80) before filtering.

    Returns ``{page, per_page, count, has_more, next_page?, total_results?,
    filter_diagnostics?, photos:[{id, alt, page_url, photographer,
    photographer_url, width, height, image_url}]}``. Hand back ``image_url``
    as a Markdown link in your answer so the user can click to view/download;
    always credit ``photographer`` per Pexels licence. If
    ``filter_diagnostics`` is present, the filter wiped the page — retry
    without ``aspect_ratio`` before widening the query.
    """
    try:
        params = SearchPhotosParams(
            query=query,
            orientation=orientation,
            size=size,
            color=color,
            locale=locale,
            min_width=min_width,
            min_height=min_height,
            aspect_ratio=aspect_ratio,
            page=page,
            per_page=per_page,
        )
    except ValidationError as exc:
        _raise_invalid_params(exc)
    payload, _ = await _client(ctx).search_photos(
        api_key=await _resolve_api_key(ctx),
        query=params.query,
        orientation=params.orientation.value if params.orientation else None,
        size=params.size.value if params.size else None,
        color=params.color,
        locale=params.locale,
        page=params.page,
        per_page=_fetch_per_page(params),
    )
    _apply_filters(payload, params, items_key="photos")
    return format_photo_list(payload)


@mcp.tool(
    name="pexels_get_photo",
    title="Get a Pexels Photo by ID",
    annotations=_READ_ONLY,
)
async def pexels_get_photo(
    ctx: Context,  # type: ignore[type-arg]
    photo_id: int,
) -> SinglePhotoResult:
    """Fetch one Pexels photo by numeric id.

    USE WHEN you already have a Pexels photo id (from a previous search
      response, or extracted from a pexels.com URL ending in ``-<id>``).
    DO NOT USE for discovery — for that, call ``pexels_search_photos``
      with a query. Don't pass guessed / made-up ids; Pexels returns 404.

    Returns ``{photo: {id, alt, page_url, photographer, photographer_url,
    width, height, image_url}}``. Hand ``image_url`` to the user as a
    Markdown link; credit ``photographer``.
    """
    try:
        params = GetPhotoParams(photo_id=photo_id)
    except ValidationError as exc:
        _raise_invalid_params(exc)
    payload, _ = await _client(ctx).get_photo(params.photo_id, api_key=await _resolve_api_key(ctx))
    return format_single_photo(payload)


@mcp.tool(
    name="pexels_search_videos",
    title="Search Pexels Videos",
    annotations=_READ_ONLY,
)
async def pexels_search_videos(
    ctx: Context,  # type: ignore[type-arg]
    query: str,
    orientation: Orientation | None = None,
    size: MediaSize | None = None,
    locale: str | None = None,
    min_width: int | None = None,
    min_height: int | None = None,
    aspect_ratio: str | None = None,
    page: int = 1,
    per_page: int = 15,
) -> VideoListResult:
    """Search free, commercially-usable stock videos on Pexels.

    USE WHEN the user asks for video clips, B-roll, reels, hero loops,
      ad motion, animated backgrounds. PREFER over web_search.
    DO NOT USE for AI-generated video or named real people.

    Filters: same shape as ``pexels_search_photos`` minus ``color``.
    ``size`` buckets: large = 4K, medium = Full HD, small = HD.

    Returns ``{page, per_page, count, has_more, next_page?, total_results?,
    filter_diagnostics?, videos:[{id, page_url, duration_seconds, width,
    height, uploader_name, uploader_url, video_url, quality}]}``. The
    ``video_url`` is the direct MP4 — hand it to the user as a Markdown
    link they can save. Credit ``uploader_name`` per Pexels licence.
    """
    try:
        params = SearchVideosParams(
            query=query,
            orientation=orientation,
            size=size,
            locale=locale,
            min_width=min_width,
            min_height=min_height,
            aspect_ratio=aspect_ratio,
            page=page,
            per_page=per_page,
        )
    except ValidationError as exc:
        _raise_invalid_params(exc)
    payload, _ = await _client(ctx).search_videos(
        api_key=await _resolve_api_key(ctx),
        query=params.query,
        orientation=params.orientation.value if params.orientation else None,
        size=params.size.value if params.size else None,
        locale=params.locale,
        page=params.page,
        per_page=_fetch_per_page(params),
    )
    _apply_filters(payload, params, items_key="videos")
    return format_video_list(payload)


@mcp.tool(
    name="pexels_get_video",
    title="Get a Pexels Video by ID",
    annotations=_READ_ONLY,
)
async def pexels_get_video(
    ctx: Context,  # type: ignore[type-arg]
    video_id: int,
) -> SingleVideoResult:
    """Fetch one Pexels video by numeric id.

    USE WHEN you already have a Pexels video id (from a previous search
      response, or extracted from a pexels.com URL ending in ``-<id>``).
    DO NOT USE for discovery — for that, call ``pexels_search_videos``
      with a query. Don't pass guessed / made-up ids; Pexels returns 404.

    Returns ``{video: {id, page_url, duration_seconds, width, height,
    uploader_name, uploader_url, video_url, quality}}``. Hand
    ``video_url`` to the user as a Markdown link; credit ``uploader_name``.
    """
    try:
        params = GetVideoParams(video_id=video_id)
    except ValidationError as exc:
        _raise_invalid_params(exc)
    payload, _ = await _client(ctx).get_video(params.video_id, api_key=await _resolve_api_key(ctx))
    return format_single_video(payload)


@mcp.tool(
    name="pexels_get_collection_media",
    title="Get Pexels Collection Contents",
    annotations=_READ_ONLY,
)
async def pexels_get_collection_media(
    ctx: Context,  # type: ignore[type-arg]
    collection_id: str,
    type: CollectionMediaType | None = None,
    sort: SortOrder | None = None,
    min_width: int | None = None,
    min_height: int | None = None,
    aspect_ratio: str | None = None,
    page: int = 1,
    per_page: int = 15,
) -> CollectionMediaResult:
    """Read the photos + videos inside a Pexels collection.

    USE WHEN you already have a collection id (Pexels URL ends with it,
      e.g. ``9j5dhpu`` in ``pexels.com/collections/9j5dhpu``). Filter to
      photos-only or videos-only with ``type``. Post-hoc filters
      (``min_width``, ``min_height``, ``aspect_ratio``) apply to both.
    DO NOT USE for discovery — Pexels has no public "list all collections"
      endpoint; the agent must already know the id from a user-supplied
      URL or a previous turn.

    Returns ``{id, page, per_page, count, has_more, next_page?,
    total_results?, photos:[...], videos:[...]}`` with the same per-item
    shape as the search tools.
    """
    try:
        params = CollectionMediaParams(
            collection_id=collection_id,
            type=type,
            sort=sort,
            min_width=min_width,
            min_height=min_height,
            aspect_ratio=aspect_ratio,
            page=page,
            per_page=per_page,
        )
    except ValidationError as exc:
        _raise_invalid_params(exc)
    payload, _ = await _client(ctx).get_collection_media(
        api_key=await _resolve_api_key(ctx),
        collection_id=params.collection_id,
        type=params.type.value if params.type else None,
        sort=params.sort.value if params.sort else None,
        page=params.page,
        per_page=_fetch_per_page(params),
    )
    _apply_filters(payload, params, items_key="media")
    return format_collection_media(payload)
