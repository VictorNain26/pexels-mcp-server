"""CLI entry point for ``pexels-mcp-server``.

Reads transport and port from environment variables, configures stderr-only
logging (stdio transport requires stdout to be JSON-RPC clean), then hands
control to FastMCP.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Literal

Transport = Literal["stdio", "streamable-http"]


def _configure_logging() -> None:
    """Send every log line to stderr. stdout is reserved for JSON-RPC."""
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="[pexels-mcp] %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _resolve_transport() -> Transport:
    raw = os.environ.get("TRANSPORT", "stdio").strip().lower()
    if raw in ("stdio", "streamable-http"):
        return raw  # type: ignore[return-value]
    sys.stderr.write(
        f"[pexels-mcp] ERROR Unknown TRANSPORT='{raw}'. Use 'stdio' or 'streamable-http'.\n"
    )
    sys.exit(2)


def main() -> None:
    """Boot the FastMCP server with the configured transport.

    The Pexels API key is no longer required at startup. Stdio callers can
    set ``PEXELS_API_KEY`` in their env so every tool call picks it up; HTTP
    callers send ``X-Pexels-Api-Key`` per request. If neither is provided the
    server still starts and tools surface an actionable error on call.
    """
    _configure_logging()
    logger = logging.getLogger("pexels_mcp_server")

    transport = _resolve_transport()
    env_key_present = bool(os.environ.get("PEXELS_API_KEY", "").strip())
    if transport == "stdio" and not env_key_present:
        logger.warning(
            "PEXELS_API_KEY is not set. Stdio clients must provide a key via "
            "env var; tools will return an auth error until it is set."
        )

    # Importing here keeps ``python -m pexels_mcp_server --help`` light and
    # defers the FastMCP/httpx import cost until we actually need it.
    from .server import mcp

    if transport == "stdio":
        logger.info("Starting pexels-mcp-server on stdio transport")
        mcp.run(transport="stdio")
        return

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    auth_token = os.environ.get("MCP_AUTH_TOKEN", "").strip()

    # Build the Starlette app and layer middleware before running uvicorn.
    # This is the only way to insert the Pexels-key extractor, the Bearer
    # gate and a /healthz endpoint without forking FastMCP.
    from typing import cast

    import uvicorn

    from .transport import (
        ASGIApp,
        bearer_auth_middleware,
        healthz_middleware,
        pexels_key_middleware,
    )

    # Starlette implements the ASGI3 protocol but its type is not
    # interchangeable with the bare callable signature mypy infers for
    # ASGIApp. Cast once at the boundary; downstream middleware stays typed.
    app: ASGIApp = cast(ASGIApp, mcp.streamable_http_app())
    # Order from outermost to innermost wrap: healthz -> bearer -> pexels_key
    # -> FastMCP. Reverse order in code because each wrap returns the new
    # outer app.
    app = pexels_key_middleware(app)
    if auth_token:
        app = bearer_auth_middleware(app, auth_token)
        logger.info("Bearer auth enabled (MCP_AUTH_TOKEN is set).")
    else:
        logger.warning(
            "MCP_AUTH_TOKEN is not set. The /mcp endpoint is open to anyone "
            "who can reach this host. Set MCP_AUTH_TOKEN before exposing "
            "the server publicly."
        )
    app = healthz_middleware(app)

    if env_key_present:
        logger.warning(
            "PEXELS_API_KEY is set on the server process. In HTTP mode this "
            "becomes a fallback for callers who do not send X-Pexels-Api-Key. "
            "Unset it if you want every caller to supply their own key."
        )

    logger.info(
        "Starting pexels-mcp-server on streamable-http transport (%s:%d)",
        host,
        port,
    )
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
