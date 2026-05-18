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
    """Boot the FastMCP server with the configured transport."""
    _configure_logging()
    logger = logging.getLogger("pexels_mcp_server")

    if not os.environ.get("PEXELS_API_KEY", "").strip():
        sys.stderr.write(
            "[pexels-mcp] ERROR Pexels API key is invalid or missing. "
            "Set PEXELS_API_KEY env var. Get a key at https://www.pexels.com/api/\n"
        )
        sys.exit(1)

    transport = _resolve_transport()

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
    # This is the only way to insert auth and a /healthz endpoint without
    # forking FastMCP.
    from typing import cast

    import uvicorn

    from .transport import ASGIApp, bearer_auth_middleware, healthz_middleware

    # Starlette implements the ASGI3 protocol but its type is not
    # interchangeable with the bare callable signature mypy infers for
    # ASGIApp. Cast once at the boundary; downstream middleware stays typed.
    app: ASGIApp = cast(ASGIApp, mcp.streamable_http_app())
    app = healthz_middleware(app)
    if auth_token:
        app = bearer_auth_middleware(app, auth_token)
        logger.info("Bearer auth enabled (MCP_AUTH_TOKEN is set).")
    else:
        logger.warning(
            "MCP_AUTH_TOKEN is not set. The /mcp endpoint is open to anyone "
            "who can reach this host. Set MCP_AUTH_TOKEN before exposing "
            "the server publicly."
        )

    logger.info(
        "Starting pexels-mcp-server on streamable-http transport (%s:%d)",
        host,
        port,
    )
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
