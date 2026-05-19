"""CLI entry point for ``pexels-mcp-server``.

Reads transport and port from environment variables, configures stderr-only
logging (stdio transport requires stdout to be JSON-RPC clean), then hands
control to FastMCP.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Literal

Transport = Literal["stdio", "streamable-http"]


class _JsonFormatter(logging.Formatter):
    """Compact JSON log formatter for hosted (HTTP) deployments.

    Designed for one-line-per-record ingestion by Koyeb / Fly / Cloud Run
    log drains. No new dependency required.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _resolve_log_format(transport: Transport) -> str:
    """Pick the log format. Explicit LOG_FORMAT env wins; otherwise default
    to JSON in HTTP mode (Koyeb log filtering) and text in stdio mode
    (so the Claude Desktop / Cursor stderr stays readable to humans).
    """
    explicit = os.environ.get("LOG_FORMAT", "").strip().lower()
    if explicit in ("json", "text"):
        return explicit
    return "json" if transport == "streamable-http" else "text"


def _configure_logging(fmt: str = "text") -> None:
    """Send every log line to stderr. stdout is reserved for JSON-RPC."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if fmt == "json":
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonFormatter())
        logging.basicConfig(level=level, handlers=[handler], force=True)
        return
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
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
    transport = _resolve_transport()
    _configure_logging(_resolve_log_format(transport))
    logger = logging.getLogger("pexels_mcp_server")
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
    # Refuse to boot HTTP mode without a Bearer token. An open /mcp endpoint
    # on a public host burns the operator's Pexels quota and exposes the
    # server to anyone on the internet. Local-loopback dev callers can opt
    # in via MCP_ALLOW_UNAUTHED=1.
    if not auth_token and os.environ.get("MCP_ALLOW_UNAUTHED", "").strip() != "1":
        sys.stderr.write(
            "[pexels-mcp] ERROR MCP_AUTH_TOKEN is required in streamable-http mode. "
            "Generate one with `openssl rand -hex 32` and set it in the environment. "
            "Set MCP_ALLOW_UNAUTHED=1 to override (development only).\n"
        )
        sys.exit(2)

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
        # Only reachable when MCP_ALLOW_UNAUTHED=1. Loud warning so the
        # operator never forgets they shipped an open endpoint.
        logger.warning(
            "MCP_ALLOW_UNAUTHED=1: Bearer auth is DISABLED. The /mcp endpoint "
            "is open to anyone who can reach this host. Do not expose this "
            "process to the public internet."
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
    # Graceful shutdown: give in-flight tool calls 25s to finish before
    # SIGKILL. Koyeb sends SIGTERM and waits ~30s by default before the kill,
    # so 25s leaves a 5s buffer for uvicorn's own teardown. Fly defaults to
    # the same 30s window. A Pexels search round-trip is typically under 1s
    # so 25s is generous; lowering it just risks dropping in-flight requests
    # during a rolling deploy.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=25,
    )


if __name__ == "__main__":
    main()
