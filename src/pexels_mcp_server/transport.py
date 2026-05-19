"""ASGI middleware for the Streamable HTTP transport.

Two pieces live here:

1. ``healthz_middleware`` — short-circuits ``GET /healthz`` and ``GET /readyz``
   so platform probes do not exercise the MCP routes (which would 405 or
   trigger an OAuth challenge).
2. ``pexels_key_middleware`` — extracts the per-request ``X-Pexels-Api-Key``
   header into a ``ContextVar`` so the tool handlers can resolve the caller's
   own Pexels key without ever storing it in the server config.

OAuth Bearer validation is **not** done here — FastMCP wraps the ``/mcp``
endpoint with its own ``RequireAuthMiddleware`` once a ``token_verifier`` is
configured, and emits the spec-compliant ``WWW-Authenticate`` header pointing
to the RFC 9728 Protected Resource Metadata URL.

The stdio transport bypasses both middlewares and reads ``PEXELS_API_KEY``
straight from the environment.
"""

from __future__ import annotations

import logging
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
