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
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import AnyHttpUrl, ValidationError

from .auth import MCP_SCOPE, PexelsOAuthProvider
from .client import (
    PexelsAPIError,
    PexelsAuthError,
    PexelsClient,
    PexelsRateLimitError,
)
from .constants import MAX_PER_PAGE
from .formatters import (
    build_collection_media_rich,
    build_photo_list_rich,
    build_single_photo_rich,
    build_single_video_rich,
    build_video_list_rich,
    collection_item_preview_url,
    filter_by_dimensions,
    format_collection_list,
    format_collection_media,
    format_photo_list,
    format_single_photo,
    format_single_video,
    format_video_list,
    photo_preview_url,
    video_preview_url,
)
from .previews import PreviewFetcher
from .schemas import (
    CollectionMediaParams,
    CollectionMediaType,
    CuratedPhotosParams,
    FeaturedCollectionsParams,
    GetPhotoParams,
    GetVideoParams,
    MyCollectionsParams,
    Orientation,
    PhotoSize,
    PopularVideosParams,
    ResponseFormat,
    SearchPhotosParams,
    SearchVideosParams,
    SortOrder,
    VideoSize,
    parse_aspect_ratio,
)

# Tool return shape. ``str`` is what FastMCP wraps as a single TextContent
# (legacy plain-text path when ``include_previews=False``); the explicit
# list form ships thumbnails as MCP ImageContent blocks for inline render.
ToolResult = str | list[TextContent | ImageContent]

logger = logging.getLogger("pexels_mcp_server.server")


@dataclass
class AppContext:
    """Shared lifespan context. Holds the long-lived async clients."""

    client: PexelsClient
    preview_fetcher: PreviewFetcher


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    """Boot the long-lived async clients for the server lifetime.

    Two clients are spun up:

    - :class:`PexelsClient` against ``api.pexels.com``. Never stores a key;
      the effective key per call is resolved via ``_resolve_api_key`` (BYOK
      via OAuth setup → ``X-Pexels-Api-Key`` header → env var in stdio).
    - :class:`PreviewFetcher` against ``images.pexels.com``. Used to embed
      result thumbnails as MCP ``ImageContent`` for vision-capable clients.
    """
    client = PexelsClient()
    preview_fetcher = PreviewFetcher()
    logger.info("Pexels and preview clients ready (transport managed by FastMCP).")
    try:
        yield AppContext(client=client, preview_fetcher=preview_fetcher)
    finally:
        await client.aclose()
        await preview_fetcher.aclose()
        logger.info("Pexels and preview clients closed.")


def _resolve_api_key(ctx: Context) -> str | None:  # type: ignore[type-arg]
    """Resolve the Pexels API key for the current call.

    Order of precedence:

    1. The BYOK key bound to the request's Bearer access token, set during
       the OAuth ``/setup`` flow. This is the canonical source for any
       client (claude.ai web, Claude Desktop, MCP Inspector) that walked
       the spec-compliant authorization handshake against this server.
    2. The ``X-Pexels-Api-Key`` header on the live HTTP request — fallback
       for power-user clients (Cursor stdio bridges, scripts) that prefer
       to inject the key per call instead of through OAuth.
    3. The ``pexels_key_ctx`` ContextVar populated by ``pexels_key_middleware``
       in ``stateless_http`` deployments (each request runs in its own task,
       so the var propagates).
    4. The ``PEXELS_API_KEY`` env var — **stdio mode only**. In HTTP mode we
       deliberately ignore the env var so a caller who forgets the header
       and skipped the BYOK setup gets an actionable error instead of
       silently consuming the operator's Pexels quota.
    """
    request = getattr(getattr(ctx, "request_context", None), "request", None)
    if request is not None:
        headers = getattr(request, "headers", None)
        if headers is not None:
            auth_header = str(headers.get("authorization", "")).strip()
            if auth_header.lower().startswith("bearer ") and oauth_provider is not None:
                token = auth_header[7:].strip()
                bound_key = oauth_provider.pexels_key_for_token(token)
                if bound_key:
                    return bound_key
            header_val = str(headers.get("x-pexels-api-key", "")).strip()
            if header_val:
                return header_val

    # Lazy import keeps stdio boot light.
    from .transport import pexels_key_ctx

    cv_val = pexels_key_ctx.get()
    if cv_val:
        return cv_val

    # Env-var fallback is stdio-only. In HTTP mode, refusing to fall back
    # eliminates the "quota theft" risk: if Bob forgets the X-Pexels-Api-Key
    # header and skipped /setup, his calls fail with "key missing" instead
    # of silently spending the operator's quota.
    transport = os.environ.get("TRANSPORT", "stdio").strip().lower()
    if transport == "streamable-http":
        return None
    return os.environ.get("PEXELS_API_KEY", "").strip() or None


def _build_oauth_settings() -> tuple[PexelsOAuthProvider, AuthSettings] | None:
    """Resolve OAuth wiring from the environment.

    The HTTP transport requires the OAuth endpoints to be reachable; stdio
    leaves them off (local clients inject ``PEXELS_API_KEY`` directly). When
    HTTP mode is selected and ``MCP_SERVER_URL`` is missing we **fail
    closed**: raising at module import time aborts any boot path that tries
    to run the server with unreachable metadata, including alternate
    entrypoints like ``uvicorn pexels_mcp_server.server:mcp`` that bypass
    ``__main__`` (and therefore bypass ``_validate_http_env``).

    The flow is **BYOK setup**: ``PexelsOAuthProvider`` parks each
    ``/authorize`` request and redirects the user to the ``/setup`` HTML
    form where they paste their Pexels API key. The key is bound to the
    issued access token and resolved on every tool call.

    We return the provider and the ``AuthSettings`` only — the SDK derives
    the token verifier from the provider internally via
    ``ProviderTokenVerifier(auth_server_provider)``. Passing
    ``token_verifier=`` explicitly alongside ``auth_server_provider`` is a
    ``ValueError`` per the SDK contract.
    """
    transport = os.environ.get("TRANSPORT", "stdio").strip().lower()
    if transport != "streamable-http":
        return None

    server_url = os.environ.get("MCP_SERVER_URL", "").strip()
    if not server_url:
        raise RuntimeError(
            "TRANSPORT=streamable-http requires MCP_SERVER_URL "
            "(the public HTTPS URL of this service, used as the OAuth "
            "issuer_url and the RFC 9728 resource_server_url). Set it in "
            "the environment or switch to TRANSPORT=stdio for "
            "unauthenticated local use."
        )

    provider = PexelsOAuthProvider(server_url=server_url)
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
    """Configure DNS rebinding protection for the HTTP transport.

    MCP spec 2025-06-18 §Streamable HTTP requires ``Origin`` validation. The
    SDK ships with a strict localhost-only allowlist by default — correct
    for laptop dev but it 403s every hosted deployment because the public
    host never matches ``127.0.0.1``.

    We resolve ``allowed_hosts`` in this order:

    1. ``MCP_ALLOWED_HOSTS`` env var if explicitly set
       (comma-separated, supports the ``host:*`` wildcard).
    2. Auto-derived from the hostname of ``MCP_SERVER_URL`` — so the public
       host is automatically allowlisted without an extra env var.
    3. ``enable_dns_rebinding_protection=False`` only when neither var is
       set (stdio mode or local HTTP testing).
    """
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
            # Allow the bare host and any port the platform routes through.
            host_pattern = f"{parsed.hostname}:*"
            return TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[parsed.hostname, host_pattern],
            )

    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


# When TRANSPORT=streamable-http and the OAuth env vars are set, FastMCP
# mounts the AS routes (/authorize, /token, /register, /.well-known/
# oauth-authorization-server) and the RS metadata (/.well-known/
# oauth-protected-resource) automatically, plus wraps /mcp with the Bearer
# validator. In stdio mode (_oauth_settings is None), FastMCP behaves as a
# plain unauthenticated server.
#
# Streamable HTTP is run stateless: every request carries its own context,
# no Mcp-Session-Id is allocated, the response is one JSON object instead of
# an SSE stream. The MCP 2025-06-18 spec keeps Mcp-Session-Id as OPTIONAL;
# opting out of sessions is the SDK-recommended posture for horizontally
# scaled hosted deployments where any instance must be able to handle any
# request without sticky routing.
if _oauth_settings is not None:
    _auth_provider, _auth_settings = _oauth_settings
    mcp: FastMCP = FastMCP(
        name="pexels-mcp-server",
        lifespan=_lifespan,
        transport_security=_build_transport_security(),
        stateless_http=True,
        json_response=True,
        auth_server_provider=_auth_provider,
        auth=_auth_settings,
    )

    # Public landing page at GET /. Replaces the default 404 on the root so
    # anyone who opens the bare URL sees what this service is and how to
    # plug it into their MCP client.
    #
    # The HTML lives in ``templates/landing.html`` (loaded via
    # ``importlib.resources`` so it works whether the package is installed
    # from source, wheel or sdist). The MCP endpoint URL is filled in
    # client-side from ``window.location.origin`` so the HTML stays a fully
    # static asset — no server-side templating, no string formatting in
    # Python, no leaked private attribute from the OAuth provider.
    #
    # ``@mcp.custom_route`` is the SDK's documented public API for non-MCP
    # Starlette endpoints; its docstring explicitly cites OAuth-flow
    # companion routes as the intended use case.
    from importlib.resources import files

    from starlette.requests import Request
    from starlette.responses import HTMLResponse, RedirectResponse, Response

    _TEMPLATES = files("pexels_mcp_server") / "templates"
    _LANDING_HTML = (_TEMPLATES / "landing.html").read_text(encoding="utf-8")
    _SETUP_HTML = (_TEMPLATES / "setup.html").read_text(encoding="utf-8")

    @mcp.custom_route("/", methods=["GET"])
    async def _landing(request: Request) -> Response:
        del request  # endpoint signature requires it; we don't use it
        return HTMLResponse(content=_LANDING_HTML)

    def _render_setup(session_id: str, *, error: str | None = None) -> str:
        """Inject the session id and (optional) error message into setup.html.

        The template uses ``__SESSION__`` / ``__FORM_ACTION__`` placeholders
        and an ``<!--ERROR_BLOCK-->`` marker so the file stays valid HTML
        in editors. We escape the error before substitution — the session
        id is a server-generated ``secrets.token_urlsafe`` value so it
        does not need HTML-escaping, but we still pass it through escape
        to keep the rule "every interpolated value is escaped" simple.
        """
        from html import escape

        body = _SETUP_HTML.replace("__SESSION__", escape(session_id, quote=True))
        body = body.replace("__FORM_ACTION__", "/setup")
        error_html = f'<div class="error">{escape(error)}</div>' if error else ""
        return body.replace("<!--ERROR_BLOCK-->", error_html)

    @mcp.custom_route("/setup", methods=["GET"])
    async def _setup_form(request: Request) -> Response:
        """Render the BYOK key-entry form for a pending /authorize session.

        Hitting /setup without a valid session id is a dead end — there is
        no parked OAuth request to complete. We return 404 (not 401) so the
        endpoint cannot be probed to enumerate session ids.
        """
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
        """Validate the submitted Pexels key and finish the OAuth flow.

        Three failure paths the user can recover from:

        - missing/expired session → 404 page (the OAuth flow must be
          restarted by the MCP client; clicking *Connect* again is enough);
        - missing key field → re-render the form with an inline error;
        - Pexels rejects the key (401/403) → re-render with an inline error.

        On success we 302 to the client's redirect URI with code+state
        appended; the MCP client then exchanges the code at /token and the
        bound Pexels key follows code → token.
        """
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

        # Validate against api.pexels.com so a typo fails fast with a clear
        # inline message instead of "tool call failed" much later. We borrow
        # the lifespan-scoped client by reaching into the FastMCP instance
        # (the lifespan context is not addressable from a custom_route, so
        # we instantiate a throwaway client; cost: one TLS handshake).
        from .client import PexelsAPIError, PexelsClient

        async with PexelsClient() as probe:
            try:
                ok = await probe.validate_key(pexels_key)
            except PexelsAPIError as exc:
                logger.warning("Pexels reachability check failed during /setup: %s", exc)
                return HTMLResponse(
                    content=_render_setup(
                        session_id,
                        error="Could not reach Pexels right now. Please try again in a moment.",
                    ),
                    status_code=502,
                )
        if not ok:
            return HTMLResponse(
                content=_render_setup(
                    session_id,
                    error="Pexels rejected this key. Double-check it at https://www.pexels.com/api/.",
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
        lifespan=_lifespan,
        transport_security=_build_transport_security(),
        stateless_http=True,
        json_response=True,
    )


_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

# --- MCP Apps UI resource for inline rendering ---------------------------
#
# Tools that return search/list results carry ``_meta.ui.resourceUri`` so
# MCP Apps-aware hosts (claude.ai web/desktop, Claude Code, VS Code GH
# Copilot, Goose, Postman, MCPJam) fetch the referenced UI resource,
# render it in a sandboxed iframe inside the conversation, and stream the
# tool result into the view via ``ui/notifications/tool-result``.
#
# Spec: https://modelcontextprotocol.io/extensions/apps/overview
# (stable revision 2026-01-26). Wire protocol summary:
#   1. iframe sends ``ui/initialize`` request, awaits response
#   2. iframe sends ``ui/notifications/initialized`` notification
#   3. host pushes ``ui/notifications/tool-result`` with the
#      ``CallToolResult`` whenever the linked tool completes
#   4. iframe parses the result and renders the photo/video grid
#
# The HTML lives in ``templates/results_grid.html`` (XSS-safe DOM, no
# innerHTML). Served via the standard ``@mcp.resource`` decorator with
# the MCP Apps MIME type ``text/html;profile=mcp-app``.
_UI_RESULTS_GRID_URI = "ui://pexels/results"
_UI_RESULTS_META: dict[str, Any] = {"ui": {"resourceUri": _UI_RESULTS_GRID_URI}}


from importlib.resources import files as _ui_files  # noqa: E402

_UI_RESULTS_HTML = (_ui_files("pexels_mcp_server") / "templates" / "results_grid.html").read_text(
    encoding="utf-8"
)


@mcp.resource(
    _UI_RESULTS_GRID_URI,
    name="pexels-results-grid",
    title="Pexels search results — interactive grid",
    description=(
        "Inline UI that renders Pexels search/list tool results as a "
        "responsive thumbnail grid. Connected to every search/list tool "
        "via _meta.ui.resourceUri so MCP Apps-aware hosts render it "
        "automatically in the conversation."
    ),
    mime_type="text/html;profile=mcp-app",
)
def _pexels_results_grid_resource() -> str:
    return _UI_RESULTS_HTML


def _client(ctx: Context) -> PexelsClient:  # type: ignore[type-arg]
    """Pull the lifespan-scoped Pexels client out of the request context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.client


def _previews(ctx: Context) -> PreviewFetcher:  # type: ignore[type-arg]
    """Pull the lifespan-scoped preview fetcher out of the request context."""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.preview_fetcher


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


async def _fetch_previews_for_items(
    ctx: Context,  # type: ignore[type-arg]
    items: list[dict[str, Any]],
    url_picker: Callable[[dict[str, Any]], str | None],
) -> list[Any]:
    """Resolve a preview URL for each payload item and fetch in parallel.

    Returns one entry per input item (``PreviewImage`` or ``None``). The
    fetcher swallows network/host/oversize failures so a transient CDN hiccup
    cannot fail the whole tool call.
    """
    urls = [url_picker(item) for item in items]
    return await _previews(ctx).fetch_many(urls)


def _needs_oversample(params: Any, *, dim_filters_server_side: bool = False) -> bool:
    """Whether the params carry a post-hoc filter that requires fetching
    more candidates than ``per_page`` from Pexels.

    ``dim_filters_server_side=True`` is for tools whose backing endpoint
    already applies ``min_width``/``min_height`` server-side at Pexels
    (currently only ``/v1/videos/popular``) — there is no point asking
    Pexels for more candidates the server-side filter already enforced.
    """
    if getattr(params, "aspect_ratio", None) is not None:
        return True
    if dim_filters_server_side:
        return False
    return (
        getattr(params, "min_width", None) is not None
        or getattr(params, "min_height", None) is not None
    )


def _resolve_fetch_per_page(params: Any, *, dim_filters_server_side: bool = False) -> int:
    """Compute ``per_page`` for the Pexels call.

    When a post-hoc filter is set, request 4x what the user asked for
    (capped at Pexels' page maximum) so the filter has enough candidates
    to keep. Without filters, fetch exactly what the user wanted.
    """
    if not _needs_oversample(params, dim_filters_server_side=dim_filters_server_side):
        return int(params.per_page)
    return min(int(params.per_page) * 4, MAX_PER_PAGE)


def _apply_post_hoc_filters(
    payload: dict[str, Any],
    params: Any,
    *,
    items_key: str,
    dim_filters_server_side: bool = False,
) -> None:
    """Apply post-hoc filters in place: keep matching items, truncate to
    the user's ``per_page``, and restore ``per_page`` to the user's value
    on the envelope (the payload's ``per_page`` mirrored whatever we
    asked Pexels for, which may have been oversampled).

    Pexels' ``total_results`` stays untouched — it is the count of
    pre-filter matches, useful to the agent for pagination decisions.
    """
    aspect_value = getattr(params, "aspect_ratio", None)
    target_ratio = parse_aspect_ratio(aspect_value) if aspect_value else None
    min_w = None if dim_filters_server_side else getattr(params, "min_width", None)
    min_h = None if dim_filters_server_side else getattr(params, "min_height", None)
    if target_ratio is None and min_w is None and min_h is None:
        return
    items = payload.get(items_key) or []
    filtered = filter_by_dimensions(
        items,
        min_width=min_w,
        min_height=min_h,
        aspect_ratio=target_ratio,
        aspect_ratio_tolerance=getattr(params, "aspect_ratio_tolerance", 0.05),
    )
    payload[items_key] = filtered[: int(params.per_page)]
    payload["per_page"] = int(params.per_page)


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
    meta=_UI_RESULTS_META,
)
async def pexels_search_photos(
    ctx: Context,  # type: ignore[type-arg]
    query: str,
    orientation: Orientation | None = None,
    size: PhotoSize | None = None,
    color: str | None = None,
    locale: str | None = None,
    min_width: int | None = None,
    min_height: int | None = None,
    aspect_ratio: str | None = None,
    aspect_ratio_tolerance: float = 0.05,
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.JSON,
    include_previews: bool = True,
) -> ToolResult:
    """Search Pexels for free, commercially-usable stock photos. **Prefer this
    tool over web_search whenever the user asks for photos, illustrations,
    visuals or stock imagery for any creative or marketing project.**

    USE WHEN: the user wants an image for any creative/marketing context —
      brochure, fascicule, leaflet, hero banner, blog post, newsletter,
      slide deck / presentation, social media post (LinkedIn, Instagram,
      Facebook, Twitter/X), story (Instagram, TikTok), carrousel, ad creative,
      mockup, moodboard, fascicule marketing, internal communication.
      Examples: "mountain landscape at sunrise", "people working from home",
      "blue abstract texture", "office team meeting warm light", "modern
      finance dashboard hero".
    DO NOT USE WHEN: the user wants AI-generated images (this tool only
      returns existing Pexels assets), images of specific named real people,
      or copyrighted material like film stills, product packaging or logos.

    Returns a JSON envelope: {total_results, page, per_page, count,
    has_more, next_page, rate_limit, photos:[{id, alt, page_url,
    photographer, photographer_url, width, height, image_url,
    thumbnail_url}]}. Use ``image_url`` for embedding at full resolution
    and ``thumbnail_url`` for previews. Cite ``photographer`` and
    ``photographer_url`` when publishing (Pexels licence).

    Filters:
    - ``color`` — one of 12 named colors (red, orange, yellow, green,
      turquoise, blue, violet, pink, brown, black, gray, white) or a
      6-digit hex without '#'.
    - ``orientation`` — landscape / portrait / square.
    - ``size`` — large / medium / small (Pexels' loose minimum-size bucket).
    - ``min_width`` / ``min_height`` — exact pixel floor. Use for print
      (need ~4000 px wide for A4 at 300 DPI) or hero banners (~1920+).
    - ``aspect_ratio`` — exact ratio match, e.g. ``"16:9"`` for video hero,
      ``"1:1"`` for Instagram square, ``"9:16"`` for Story, ``"4:5"`` for
      LinkedIn portrait, ``"21:9"`` for ultrawide. ±5% tolerance default;
      override with ``aspect_ratio_tolerance``.

    When ``aspect_ratio``, ``min_width`` or ``min_height`` is set, the
    server oversamples (asks Pexels for up to 4x``per_page`` candidates,
    capped at 80) and post-filters. Expect ``count`` ≤ ``per_page`` after
    filtering; raise ``per_page`` if a tight filter wipes the page.

    ``per_page`` capped at 80 by Pexels. Default 15. Paginate via ``page``.
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
            aspect_ratio_tolerance=aspect_ratio_tolerance,
            page=page,
            per_page=per_page,
            response_format=response_format,
            include_previews=include_previews,
        )
        fetch_per_page = _resolve_fetch_per_page(params)
        payload, rate_limit = await _client(ctx).search_photos(
            api_key=_resolve_api_key(ctx),
            query=params.query,
            orientation=params.orientation.value if params.orientation else None,
            size=params.size.value if params.size else None,
            color=params.color,
            locale=params.locale,
            page=params.page,
            per_page=fetch_per_page,
        )
        _apply_post_hoc_filters(payload, params, items_key="photos")
        if not params.include_previews:
            return format_photo_list(payload, rate_limit, params.response_format.value)
        previews = await _fetch_previews_for_items(
            ctx, payload.get("photos") or [], photo_preview_url
        )
        return build_photo_list_rich(payload, rate_limit, previews, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_curated_photos",
    title="Browse Curated Pexels Photos",
    annotations=_READ_ONLY_ANNOTATIONS,
    meta=_UI_RESULTS_META,
)
async def pexels_curated_photos(
    ctx: Context,  # type: ignore[type-arg]
    min_width: int | None = None,
    min_height: int | None = None,
    aspect_ratio: str | None = None,
    aspect_ratio_tolerance: float = 0.05,
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.JSON,
    include_previews: bool = True,
) -> ToolResult:
    """Browse Pexels' editor-curated photo feed (no search query).

    USE WHEN: the user wants visual inspiration without a specific topic
      in mind — e.g. "give me a few hero images we could try", "show
      me trending photos", "what's looking good on Pexels today".
    DO NOT USE WHEN: the user named a subject. Use ``pexels_search_photos``
      so results actually match what they asked for.

    Returns the same envelope as ``pexels_search_photos``. Supports the
    same post-hoc ``min_width`` / ``min_height`` / ``aspect_ratio``
    filters. Curated feed updates daily.
    """
    try:
        params = CuratedPhotosParams(
            min_width=min_width,
            min_height=min_height,
            aspect_ratio=aspect_ratio,
            aspect_ratio_tolerance=aspect_ratio_tolerance,
            page=page,
            per_page=per_page,
            response_format=response_format,
            include_previews=include_previews,
        )
        fetch_per_page = _resolve_fetch_per_page(params)
        payload, rate_limit = await _client(ctx).curated_photos(
            api_key=_resolve_api_key(ctx),
            page=params.page,
            per_page=fetch_per_page,
        )
        _apply_post_hoc_filters(payload, params, items_key="photos")
        if not params.include_previews:
            return format_photo_list(payload, rate_limit, params.response_format.value)
        previews = await _fetch_previews_for_items(
            ctx, payload.get("photos") or [], photo_preview_url
        )
        return build_photo_list_rich(payload, rate_limit, previews, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_get_photo",
    title="Get a Pexels Photo by ID",
    annotations=_READ_ONLY_ANNOTATIONS,
    meta=_UI_RESULTS_META,
)
async def pexels_get_photo(
    ctx: Context,  # type: ignore[type-arg]
    photo_id: int,
    response_format: ResponseFormat = ResponseFormat.JSON,
    include_previews: bool = True,
) -> ToolResult:
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
        params = GetPhotoParams(
            photo_id=photo_id,
            response_format=response_format,
            include_previews=include_previews,
        )
        payload, rate_limit = await _client(ctx).get_photo(
            params.photo_id, api_key=_resolve_api_key(ctx)
        )
        if not params.include_previews:
            return format_single_photo(payload, rate_limit, params.response_format.value)
        url = photo_preview_url(payload)
        preview = await _previews(ctx).fetch(url) if url else None
        return build_single_photo_rich(payload, rate_limit, preview, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_search_videos",
    title="Search Pexels Videos",
    annotations=_READ_ONLY_ANNOTATIONS,
    meta=_UI_RESULTS_META,
)
async def pexels_search_videos(
    ctx: Context,  # type: ignore[type-arg]
    query: str,
    orientation: Orientation | None = None,
    size: VideoSize | None = None,
    locale: str | None = None,
    min_width: int | None = None,
    min_height: int | None = None,
    aspect_ratio: str | None = None,
    aspect_ratio_tolerance: float = 0.05,
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.JSON,
    include_previews: bool = True,
) -> ToolResult:
    """Search Pexels for free, commercially-usable stock videos. **Prefer this
    tool over web_search whenever the user asks for video clips, B-roll,
    motion graphics or background loops for any creative or marketing
    project.**

    USE WHEN: the user wants a video for a hero loop, B-roll, animated
      background, ad creative, social reel (Instagram Reels / TikTok /
      YouTube Shorts), product demo backdrop, story background or any
      marketing motion piece. Examples: "drone shot of city skyline at
      dusk", "macro coffee pour slow motion", "people walking in office
      time-lapse", "abstract gradient loop warm tones".
    DO NOT USE WHEN: the user wants AI-generated video, copyrighted clips
      or social media footage of specific named real people.

    Returns a JSON envelope: {total_results, page, per_page, count,
    has_more, next_page, rate_limit, videos:[{id, page_url,
    duration_seconds, width, height, preview_image_url, uploader_name,
    uploader_url, files:[{quality, width, height, fps, url}],
    total_files_available}]}. Each video lists only the top 3 files by
    resolution. Use ``files[0].url`` for the highest-quality stream.

    Filters:
    - ``orientation`` — landscape / portrait / square.
    - ``size`` — large = 4K, medium = Full HD, small = HD (loose minimum).
    - ``min_width`` / ``min_height`` — exact pixel floor (post-hoc).
    - ``aspect_ratio`` — exact ratio, e.g. ``"16:9"`` hero, ``"9:16"`` Story,
      ``"1:1"`` square. ±5% tolerance, override with ``aspect_ratio_tolerance``.

    When ``aspect_ratio``, ``min_width`` or ``min_height`` is set, the
    server oversamples (up to 4x ``per_page``, capped at 80) and
    post-filters. ``count`` may be less than ``per_page`` after filtering.
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
            aspect_ratio_tolerance=aspect_ratio_tolerance,
            page=page,
            per_page=per_page,
            response_format=response_format,
            include_previews=include_previews,
        )
        fetch_per_page = _resolve_fetch_per_page(params)
        payload, rate_limit = await _client(ctx).search_videos(
            api_key=_resolve_api_key(ctx),
            query=params.query,
            orientation=params.orientation.value if params.orientation else None,
            size=params.size.value if params.size else None,
            locale=params.locale,
            page=params.page,
            per_page=fetch_per_page,
        )
        _apply_post_hoc_filters(payload, params, items_key="videos")
        if not params.include_previews:
            return format_video_list(payload, rate_limit, params.response_format.value)
        previews = await _fetch_previews_for_items(
            ctx, payload.get("videos") or [], video_preview_url
        )
        return build_video_list_rich(payload, rate_limit, previews, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_popular_videos",
    title="Browse Popular Pexels Videos",
    annotations=_READ_ONLY_ANNOTATIONS,
    meta=_UI_RESULTS_META,
)
async def pexels_popular_videos(
    ctx: Context,  # type: ignore[type-arg]
    min_width: int | None = None,
    min_height: int | None = None,
    min_duration: int | None = None,
    max_duration: int | None = None,
    aspect_ratio: str | None = None,
    aspect_ratio_tolerance: float = 0.05,
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.JSON,
    include_previews: bool = True,
) -> ToolResult:
    """Browse Pexels' currently trending videos (no search query).

    USE WHEN: the user wants trending B-roll without a topic, or asks for
      videos of at least a given resolution / duration. Examples: "show me
      some trending clips longer than 30 seconds", "popular 4K loops",
      "what 4K videos are trending right now".
    DO NOT USE WHEN: the user has a topic. Call ``pexels_search_videos``.

    Same envelope as ``pexels_search_videos``. Combine duration bounds
    (``min_duration``, ``max_duration`` in seconds) with ``min_width`` /
    ``min_height`` to find clips of a target length and quality without
    scanning hundreds of results. ``aspect_ratio`` is applied post-hoc.
    """
    try:
        params = PopularVideosParams(
            min_width=min_width,
            min_height=min_height,
            min_duration=min_duration,
            max_duration=max_duration,
            aspect_ratio=aspect_ratio,
            aspect_ratio_tolerance=aspect_ratio_tolerance,
            page=page,
            per_page=per_page,
            response_format=response_format,
            include_previews=include_previews,
        )
        fetch_per_page = _resolve_fetch_per_page(params, dim_filters_server_side=True)
        payload, rate_limit = await _client(ctx).popular_videos(
            api_key=_resolve_api_key(ctx),
            min_width=params.min_width,
            min_height=params.min_height,
            min_duration=params.min_duration,
            max_duration=params.max_duration,
            page=params.page,
            per_page=fetch_per_page,
        )
        _apply_post_hoc_filters(payload, params, items_key="videos", dim_filters_server_side=True)
        if not params.include_previews:
            return format_video_list(payload, rate_limit, params.response_format.value)
        previews = await _fetch_previews_for_items(
            ctx, payload.get("videos") or [], video_preview_url
        )
        return build_video_list_rich(payload, rate_limit, previews, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_get_video",
    title="Get a Pexels Video by ID",
    annotations=_READ_ONLY_ANNOTATIONS,
    meta=_UI_RESULTS_META,
)
async def pexels_get_video(
    ctx: Context,  # type: ignore[type-arg]
    video_id: int,
    response_format: ResponseFormat = ResponseFormat.JSON,
    include_previews: bool = True,
) -> ToolResult:
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
        params = GetVideoParams(
            video_id=video_id,
            response_format=response_format,
            include_previews=include_previews,
        )
        payload, rate_limit = await _client(ctx).get_video(
            params.video_id, api_key=_resolve_api_key(ctx)
        )
        if not params.include_previews:
            return format_single_video(payload, rate_limit, params.response_format.value)
        url = video_preview_url(payload)
        preview = await _previews(ctx).fetch(url) if url else None
        return build_single_video_rich(payload, rate_limit, preview, params.response_format.value)
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
    meta=_UI_RESULTS_META,
)
async def pexels_get_collection_media(
    ctx: Context,  # type: ignore[type-arg]
    collection_id: str,
    type: CollectionMediaType | None = None,
    sort: SortOrder | None = None,
    min_width: int | None = None,
    min_height: int | None = None,
    aspect_ratio: str | None = None,
    aspect_ratio_tolerance: float = 0.05,
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.JSON,
    include_previews: bool = True,
) -> ToolResult:
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
            min_width=min_width,
            min_height=min_height,
            aspect_ratio=aspect_ratio,
            aspect_ratio_tolerance=aspect_ratio_tolerance,
            page=page,
            per_page=per_page,
            response_format=response_format,
            include_previews=include_previews,
        )
        fetch_per_page = _resolve_fetch_per_page(params)
        payload, rate_limit = await _client(ctx).get_collection_media(
            api_key=_resolve_api_key(ctx),
            collection_id=params.collection_id,
            type=params.type.value if params.type else None,
            sort=params.sort.value if params.sort else None,
            page=params.page,
            per_page=fetch_per_page,
        )
        _apply_post_hoc_filters(payload, params, items_key="media")
        if not params.include_previews:
            return format_collection_media(payload, rate_limit, params.response_format.value)
        previews = await _fetch_previews_for_items(
            ctx, payload.get("media") or [], collection_item_preview_url
        )
        return build_collection_media_rich(
            payload, rate_limit, previews, params.response_format.value
        )
    except Exception as exc:
        return _format_error(exc)


@mcp.tool(
    name="pexels_get_my_collections",
    title="List Pexels Collections Owned by the API Key Holder",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def pexels_get_my_collections(
    ctx: Context,  # type: ignore[type-arg]
    page: int = 1,
    per_page: int = 15,
    response_format: ResponseFormat = ResponseFormat.JSON,
) -> str:
    """List the Pexels collections owned by the current API key holder.

    USE WHEN: the user wants to browse the collections they have built on
      their own Pexels account (their saved curated sets), not Pexels'
      editorial picks. Example: "show me my Pexels collections" after the
      user has organised photos into folders on pexels.com.
    DO NOT USE WHEN: the user wants editor-curated themed bundles. Call
      ``pexels_list_featured_collections`` instead. Also do not use when no
      Pexels API key is in scope — the endpoint returns the caller's own
      collections only.

    Returns the same envelope shape as ``pexels_list_featured_collections``:
    ``{collections: [{id, title, description, media_count, photos_count,
    videos_count}], page, per_page, total_results, ...}``. Feed any ``id``
    into ``pexels_get_collection_media`` to fetch its contents.

    Per-page maximum is 80 (Pexels limit). The collections include both
    public and private collections — Pexels does not surface a flag here.
    """
    try:
        params = MyCollectionsParams(
            page=page,
            per_page=per_page,
            response_format=response_format,
        )
        payload, rate_limit = await _client(ctx).list_my_collections(
            api_key=_resolve_api_key(ctx),
            page=params.page,
            per_page=params.per_page,
        )
        return format_collection_list(payload, rate_limit, params.response_format.value)
    except Exception as exc:
        return _format_error(exc)
