"""FastMCP server exposing the Pexels API to MCP-aware AI agents.

Five read-only tools: search photos / get photo / search videos / get video
/ get collection media. Each tool returns a structured ``dict`` — the SDK
auto-populates ``structuredContent`` (validated against ``outputSchema``)
and a serialized JSON ``TextContent`` block for backwards compat. Errors
raise; FastMCP wraps them with ``isError=true`` per MCP spec 2025-11-25
(SEP-1303).

Pexels free tier: 200 requests/hour, 20 000 requests/month. The server
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


# Sent once in ``serverInfo.instructions`` on initialize. Two things the
# LLM cannot infer from the tool surface alone:
#
# 1. Attribution: photographer / uploader credit is mandatory per the
#    Pexels licence.
# 2. URL handling: ``image_url`` / ``video_url`` are public CDN links —
#    render them as Markdown for inline display, or hand them to any
#    URL-accepting downstream tool. Downloading the bytes locally
#    (curl + base64) wastes the user's token budget for zero gain —
#    the bytes never become useful tokens for the LLM, they just
#    transit through the context and bloat it into overflow.
_SERVER_INSTRUCTIONS = (
    "Pexels stock photo/video search. "
    "Credit photographer/uploader per the Pexels licence. "
    "image_url and video_url are public CDN links: render them as Markdown "
    "for inline display, or pass them to any URL-accepting downstream tool "
    "(image fills, design assets, embeds). Do not download the bytes locally."
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
    """Search Pexels for free, commercially-usable stock photos.

    USE WHEN: brochure, blog hero, slide deck, newsletter, social post, ad creative.
    PREFER THIS over web_search for any stock-photo request.
    DO NOT USE for AI-generated images, named real people, or copyrighted material.

    Filters: orientation, size, color (named or hex), locale. Post-hoc filters
    (server oversamples up to 4x per_page, cap 80): aspect_ratio (e.g. "16:9"),
    min_width, min_height (~4000 for A4 print, ~1920 for hero).

    image_url is a public CDN link: render as Markdown or pass to any
    URL-accepting downstream tool. Do not curl/download the bytes.
    Always credit photographer per Pexels licence.
    filter_diagnostics present → retry without aspect_ratio first.
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
    """Fetch one Pexels photo by id.

    USE WHEN you have a photo id (previous search result, or extracted
    from a pexels.com URL ending in -<id>).
    DO NOT USE for discovery — call pexels_search_photos. No guessed ids.

    Render image_url as Markdown link; credit photographer.
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
    """Search Pexels for free, commercially-usable stock videos.

    USE WHEN: B-roll, reels, hero loops, ad motion, animated backgrounds.
    PREFER THIS over web_search for stock-video requests.
    DO NOT USE for AI-generated video or named real people.

    Filters: orientation, size (large=4K, medium=FullHD, small=HD), locale.
    Post-hoc (4x oversample, cap 80): aspect_ratio, min_width, min_height.

    video_url is a public CDN MP4 link: render as Markdown or pass to any
    URL-accepting downstream tool. Do not curl/download the bytes.
    Credit uploader_name per Pexels licence. filter_diagnostics same
    semantics as pexels_search_photos.
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
    """Fetch one Pexels video by id.

    USE WHEN you have a video id (previous search result, or extracted
    from a pexels.com URL ending in -<id>).
    DO NOT USE for discovery — call pexels_search_videos. No guessed ids.

    Render video_url as Markdown link; credit uploader_name.
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

    USE WHEN you have a collection id (pexels.com/collections/<id>).
    Filter to one type with `type` ('photos' or 'videos').
    Post-hoc filters (aspect_ratio, min_width, min_height) apply to both.
    DO NOT USE for discovery — no public list-all-collections endpoint.

    Per-item shape matches the search tools.
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


# =========================================================== resources
#
# Three URI-template resources that mirror the get_* / get_collection_media
# tools. Resources are the MCP primitive that hosts (claude.ai web, Claude
# Desktop, MCP-Apps-aware clients) can attach to the conversation directly:
# a user pasting a pexels.com URL into their chat gets the photo / video /
# collection content surfaced without the agent needing to invoke a tool.
#
# Token cost: one ResourceTemplate descriptor in ``resources/templates/list``
# per resource (~100 chars each). Roughly 300 chars at conversation init —
# trivial against the gain in user-controlled context attachment.


@mcp.resource(
    "pexels://photo/{photo_id}",
    name="pexels_photo",
    title="Pexels Photo",
    description="One photo by id.",
    mime_type="application/json",
)
async def _resource_photo(
    photo_id: str,
    ctx: Context,  # type: ignore[type-arg]
) -> SinglePhotoResult:
    try:
        params = GetPhotoParams(photo_id=int(photo_id))
    except (ValidationError, ValueError) as exc:
        raise ValueError(f"Invalid photo_id: {photo_id!r}") from exc
    payload, _ = await _client(ctx).get_photo(params.photo_id, api_key=await _resolve_api_key(ctx))
    return format_single_photo(payload)


@mcp.resource(
    "pexels://video/{video_id}",
    name="pexels_video",
    title="Pexels Video",
    description="One video by id.",
    mime_type="application/json",
)
async def _resource_video(
    video_id: str,
    ctx: Context,  # type: ignore[type-arg]
) -> SingleVideoResult:
    try:
        params = GetVideoParams(video_id=int(video_id))
    except (ValidationError, ValueError) as exc:
        raise ValueError(f"Invalid video_id: {video_id!r}") from exc
    payload, _ = await _client(ctx).get_video(params.video_id, api_key=await _resolve_api_key(ctx))
    return format_single_video(payload)


@mcp.resource(
    "pexels://collection/{collection_id}",
    name="pexels_collection",
    title="Pexels Collection",
    description="All media in a collection.",
    mime_type="application/json",
)
async def _resource_collection(
    collection_id: str,
    ctx: Context,  # type: ignore[type-arg]
) -> CollectionMediaResult:
    try:
        params = CollectionMediaParams(collection_id=collection_id)
    except ValidationError as exc:
        _raise_invalid_params(exc)
    payload, _ = await _client(ctx).get_collection_media(
        api_key=await _resolve_api_key(ctx),
        collection_id=params.collection_id,
        page=params.page,
        per_page=int(params.per_page),
    )
    return format_collection_media(payload)


# ============================================================= prompts
#
# Three reusable prompt templates surfaced in the claude.ai connector
# menu. Each one renders a short user-message brief that nudges the
# agent toward the right ``pexels_search_*`` call with the right
# filters — the user picks the prompt, fills two or three fields, and
# the LLM gets a structured request instead of free-form text. This
# tends to cut the agent's back-and-forth on parameter clarification
# (each saved round-trip beats the ~600 token cost of ``prompts/list``).


@mcp.prompt(
    name="find_hero_image",
    title="Find a hero image",
    description="Marketing hero image with brand color + aspect ratio.",
)
def _prompt_find_hero_image(
    topic: str,
    orientation: str = "landscape",
    brand_color: str | None = None,
    aspect_ratio: str = "16:9",
) -> str:
    """Brief the agent for a hero-image search."""
    extras = [f"orientation={orientation!r}", f"aspect_ratio={aspect_ratio!r}"]
    if brand_color:
        extras.append(f"color={brand_color!r}")
    return (
        f"Find a stock photo on Pexels for: {topic}.\n"
        f"Call `pexels_search_photos` with {', '.join(extras)}, min_width=1920.\n"
        "Return the best `image_url` as a Markdown link with the "
        "mandatory `photographer` credit."
    )


@mcp.prompt(
    name="find_broll",
    title="Find B-roll footage",
    description="Stock video clip for B-roll / hero loop / ad motion.",
)
def _prompt_find_broll(
    topic: str,
    orientation: str = "landscape",
    resolution: str = "4k",
) -> str:
    """Brief the agent for a B-roll video search."""
    size_hint = "large" if resolution.lower() == "4k" else "medium"
    return (
        f"Find a stock video clip on Pexels for: {topic}.\n"
        f"Call `pexels_search_videos` with orientation={orientation!r}, "
        f"size={size_hint!r}, aspect_ratio='16:9'.\n"
        "Return the best `video_url` (direct MP4) as a Markdown link "
        "with the `uploader_name` credit."
    )


@mcp.prompt(
    name="find_brand_match",
    title="Match a brand color",
    description="Search a stock photo that fits a brand hex color.",
)
def _prompt_find_brand_match(
    query: str,
    brand_hex_color: str,
) -> str:
    """Brief the agent for a color-driven photo search."""
    return (
        f"Find a stock photo on Pexels matching the brand color "
        f"#{brand_hex_color.lstrip('#')} for: {query}.\n"
        f"Call `pexels_search_photos` with color={brand_hex_color.lstrip('#')!r}.\n"
        "Return the best `image_url` as a Markdown link, credit "
        "`photographer`."
    )
