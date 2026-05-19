"""ASGI middleware for the Streamable HTTP transport.

Three pieces live here:

1. ``healthz_middleware`` — short-circuits ``GET /healthz`` and ``GET /readyz``
   so platform probes do not exercise the MCP routes (which would 405 or
   trigger an OAuth challenge).
2. ``rate_limit_middleware`` — sliding-window per source IP. Soft DoS guard;
   exempts the platform probes and the OAuth metadata so health checks and
   discovery clients are never throttled.
3. ``pexels_key_middleware`` — extracts the per-request ``X-Pexels-Api-Key``
   header into a ``ContextVar`` so the tool handlers can resolve the caller's
   own Pexels key without ever storing it in the server config.

OAuth Bearer validation is **not** done here — FastMCP wraps the ``/mcp``
endpoint with its own ``RequireAuthMiddleware`` once a ``token_verifier`` is
configured, and emits the spec-compliant ``WWW-Authenticate`` header pointing
to the RFC 9728 Protected Resource Metadata URL.

The stdio transport bypasses every middleware here and reads ``PEXELS_API_KEY``
straight from the environment.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("pexels_mcp_server.transport")

ASGIScope = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]


# Per-request Pexels API key. Populated by ``pexels_key_middleware`` from the
# ``X-Pexels-Api-Key`` request header. Reset to ``None`` outside an HTTP
# request so the stdio transport falls back to the env var.
pexels_key_ctx: ContextVar[str | None] = ContextVar("pexels_api_key", default=None)


async def _send_text(send: ASGISend, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


class _SlidingWindowLimiter:
    """Per-key sliding-window rate limiter, in-memory and process-local.

    Sized for a single-replica eco-nano Koyeb deployment. If the service
    is ever scaled horizontally the limit becomes per-replica (so the
    effective rate is N * max_hits/window), which is acceptable for a soft
    DoS guard but not for hard quotas. Move to Redis for distributed
    enforcement.
    """

    def __init__(self, max_hits: int, window_seconds: float) -> None:
        if max_hits <= 0:
            raise ValueError("max_hits must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._max = max_hits
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        # Periodic sweep of inactive keys to keep ``self._hits`` bounded under
        # high-cardinality traffic. Without it, a bot rotating one-shot IPs
        # would grow the dict forever. We sweep at most once per window —
        # an O(N) scan that runs ~once/minute on a 60s window is cheap even
        # at 100k tracked IPs.
        self._last_sweep_at: float | None = None

    async def hit(self, key: str, *, now: float | None = None) -> tuple[bool, int]:
        """Record a hit for ``key``.

        Returns ``(allowed, retry_after_seconds)``. ``retry_after`` is 0 when
        allowed. When over the limit, ``retry_after`` is the seconds until the
        oldest hit in the window expires (so the client can be told exactly
        when to come back).
        """
        async with self._lock:
            t = time.monotonic() if now is None else now
            self._maybe_sweep_inactive(t)
            hits = self._hits[key]
            cutoff = t - self._window
            # Use ``<=`` so a hit recorded at exactly ``now - window`` is
            # treated as outside the window — otherwise a hit at t=0 still
            # counts at t=60 with a 60 s window, which silently makes the
            # effective limit stricter than the configured value.
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self._max:
                retry_after = max(1, int(hits[0] + self._window - t) + 1)
                return False, retry_after
            hits.append(t)
            return True, 0

    def _maybe_sweep_inactive(self, now: float) -> None:
        """Drop tracking entries for IPs that have been quiet for a whole window.

        Called under ``self._lock`` from inside ``hit``. Runs at most once per
        ``window_seconds`` so the amortized cost is O(N/window) per hit, which
        stays negligible compared to the network round-trip the request just
        completed.
        """
        if self._last_sweep_at is not None and now - self._last_sweep_at < self._window:
            return
        self._last_sweep_at = now
        cutoff = now - self._window
        stale = [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for k in stale:
            del self._hits[k]


def _real_ip(scope: ASGIScope, *, trusted_proxy_hops: int = 1) -> str:
    """Extract the real caller IP from the ASGI scope.

    ``X-Forwarded-For`` is set by every proxy in the chain: each one appends
    the address it saw the request *come from*. The **leftmost** entry is
    therefore client-controlled (an attacker can put any string in their own
    request) and must not be trusted. The **rightmost** entry is the IP the
    last proxy saw, which is always one of *our* proxies and is therefore
    safe. Skipping ``trusted_proxy_hops`` entries from the right gives the
    address one hop *before* that proxy — the actual client when we trust the
    full chain in front of the app.

    On Koyeb the load balancer appends one entry, so ``trusted_proxy_hops=1``
    (the default) yields the client IP. Cloudflare in front of Koyeb is two
    hops; configure ``MCP_TRUSTED_PROXY_HOPS`` accordingly.

    Falls back to the socket peer address if the header is absent or the chain
    is shorter than expected.
    """
    if trusted_proxy_hops < 1:
        # Treat all of X-Forwarded-For as untrusted: ignore the header.
        forwarded_chain: list[str] = []
    else:
        forwarded_chain = []
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        for name, value in headers:
            if name.lower() == b"x-forwarded-for":
                forwarded_chain = [
                    part.strip()
                    for part in value.decode("latin-1", errors="ignore").split(",")
                    if part.strip()
                ]
                break
    # The IP we want sits at position (len - hops) from the left. Anything
    # to its right is one of our trusted proxies; anything to its left is
    # client-supplied and unreliable.
    if forwarded_chain and len(forwarded_chain) >= trusted_proxy_hops:
        return forwarded_chain[len(forwarded_chain) - trusted_proxy_hops]
    client = scope.get("client")
    if isinstance(client, tuple | list) and client:
        return str(client[0])
    return "unknown"


# Paths that bypass rate limiting:
# - Platform liveness / readiness probes (would auto-DoS the service into
#   the unhealthy state if throttled).
# - The OAuth discovery endpoints: clients hit these *before* authentication
#   to learn how to connect, so throttling them breaks the discovery flow.
_RATE_LIMIT_EXEMPT_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    }
)


def rate_limit_middleware(
    app: ASGIApp, *, max_per_minute: int, trusted_proxy_hops: int = 1
) -> ASGIApp:
    """Cap incoming HTTP requests at ``max_per_minute`` per source IP.

    Beyond the cap the middleware returns ``429 Too Many Requests`` with a
    ``Retry-After`` header per RFC 9110 §15.5.20. Probes and OAuth discovery
    are exempt; everything else (``/mcp``, ``/authorize``, ``/token``,
    ``/register``, the landing page) counts against the per-IP budget.

    ``trusted_proxy_hops`` controls how the source IP is read from
    ``X-Forwarded-For`` — see ``_real_ip``. Default is 1 (Koyeb's LB only).
    """
    limiter = _SlidingWindowLimiter(max_hits=max_per_minute, window_seconds=60.0)

    async def wrapped(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in _RATE_LIMIT_EXEMPT_PATHS:
            await app(scope, receive, send)
            return
        ip = _real_ip(scope, trusted_proxy_hops=trusted_proxy_hops)
        allowed, retry_after = await limiter.hit(ip)
        if not allowed:
            logger.warning("rate limit hit by %s on %s, retry in %ds", ip, path, retry_after)
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"retry-after", str(retry_after).encode()),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"rate limit exceeded\n",
                    "more_body": False,
                }
            )
            return
        await app(scope, receive, send)

    return wrapped


def healthz_middleware(app: ASGIApp) -> ASGIApp:
    """Short-circuit ``GET /healthz`` and ``GET /readyz`` with ``200 ok``.

    ``/healthz`` is the liveness probe — returns 200 as soon as the process
    is up. ``/readyz`` is the readiness probe — same shape today, exposed on
    a separate path so platforms can wire each probe independently and we
    can grow the readiness check later (e.g. ping Pexels) without affecting
    liveness semantics.
    """

    async def wrapped(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope.get("type") == "http" and scope.get("path") in ("/healthz", "/readyz"):
            await _send_text(send, 200, b"ok")
            return
        await app(scope, receive, send)

    return wrapped


def pexels_key_middleware(app: ASGIApp) -> ASGIApp:
    """Extract ``X-Pexels-Api-Key`` from the request headers into a ContextVar.

    The Pexels key is never part of the server's static config: each caller
    sends their own key with every request. This middleware reads the header
    once per request and lets the tool handlers pick it up from the context
    var; the value is reset right after the downstream app runs so nothing
    leaks across requests (uvicorn already isolates ContextVars per task,
    but resetting is cheap and defensive).
    """

    async def wrapped(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        key_value: str | None = None
        for name, value in headers:
            if name.lower() == b"x-pexels-api-key":
                key_value = value.decode("latin-1", errors="ignore").strip() or None
                break
        token = pexels_key_ctx.set(key_value)
        logger.debug(
            "pexels_key_middleware: %s key on %s",
            "set" if key_value else "no",
            scope.get("path"),
        )
        try:
            await app(scope, receive, send)
        finally:
            pexels_key_ctx.reset(token)

    return wrapped
