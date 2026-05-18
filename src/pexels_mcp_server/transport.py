"""ASGI middleware used by the Streamable HTTP transport.

Three things live here:

1. ``healthz_middleware`` - short-circuit ``GET /healthz`` so platform liveness
   probes never hit the MCP routes (which would 405 / 401).
2. ``bearer_auth_middleware`` - shared-secret Bearer gate driven by the
   ``MCP_AUTH_TOKEN`` env var. Protects the host's CPU / bandwidth from random
   internet traffic.
3. ``pexels_key_middleware`` - extracts the per-request ``X-Pexels-Api-Key``
   header into a ``ContextVar`` so the tool handlers can resolve the caller's
   own Pexels key without it ever being part of the server's static config.

The stdio transport bypasses all three and keeps using the ``PEXELS_API_KEY``
env var as the only source of truth, which matches how local clients
(Claude Desktop, Claude Code) inject the key today.
"""

from __future__ import annotations

import hmac
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


def _extract_bearer(scope: ASGIScope) -> str | None:
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for name, value in headers:
        if name.lower() == b"authorization":
            decoded: str = value.decode("latin-1", errors="ignore").strip()
            if decoded.lower().startswith("bearer "):
                return decoded[7:].strip()
            return None
    return None


def healthz_middleware(app: ASGIApp) -> ASGIApp:
    """Short-circuit ``GET /healthz`` so platform health probes do not exercise
    the MCP transport (which would 405 on a plain GET).
    """

    async def wrapped(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/healthz":
            await _send_text(send, 200, b"ok")
            return
        await app(scope, receive, send)

    return wrapped


def pexels_key_middleware(app: ASGIApp) -> ASGIApp:
    """Extract ``X-Pexels-Api-Key`` from the request headers into a ContextVar.

    The Pexels key never sits in the server's static config: each caller has
    to send their own key with every request. This middleware reads the
    header once per request and lets the tool handlers pick it up from the
    contextvar; the value is reset right after the downstream app runs so
    nothing leaks across requests (uvicorn already isolates contextvars per
    task, but resetting is cheap and defensive).
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


def bearer_auth_middleware(app: ASGIApp, expected_token: str) -> ASGIApp:
    """Reject HTTP requests without ``Authorization: Bearer <expected_token>``.

    The token is compared with ``hmac.compare_digest`` to dodge trivial timing
    side channels. Non-HTTP scopes (websocket, lifespan) pass through.
    """

    expected = expected_token.encode("utf-8")

    async def wrapped(scope: ASGIScope, receive: ASGIReceive, send: ASGISend) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        if scope.get("path") == "/healthz":
            await app(scope, receive, send)
            return
        presented = _extract_bearer(scope)
        if presented is None or not hmac.compare_digest(presented.encode("utf-8"), expected):
            logger.warning(
                "rejected unauthenticated request to %s from %s",
                scope.get("path"),
                scope.get("client"),
            )
            await _send_text(send, 401, b"unauthorized\n")
            return
        await app(scope, receive, send)

    return wrapped
