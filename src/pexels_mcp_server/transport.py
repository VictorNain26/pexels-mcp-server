"""ASGI middleware used by the Streamable HTTP transport.

The MCP protocol does not mandate Bearer auth on the transport, but a public
HTTP deployment without any gate gives anyone who guesses the URL free reign
over the operator's Pexels quota. This middleware adds a minimal shared-secret
Bearer check and a ``/healthz`` liveness probe so platforms like Koyeb can
verify the container is ready.

The middleware is a no-op when ``MCP_AUTH_TOKEN`` is unset, so the stdio
transport and local dev deployments keep their zero-config feel.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("pexels_mcp_server.transport")

ASGIScope = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[ASGIScope, ASGIReceive, ASGISend], Awaitable[None]]


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
