"""CLI entry point for ``pexels-mcp-server``.

Reads ``TRANSPORT`` from the environment, configures stderr-only logging
(stdio transport requires stdout to be JSON-RPC clean), then hands control
to FastMCP. The hosted HTTP transport additionally wires OAuth 2.1 (RS+AS
in a single process via the MCP SDK), the ``/login`` form for the human
passcode step, and the ``X-Pexels-Api-Key`` extractor middleware.
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
    to JSON in HTTP mode (Koyeb log filtering) and text in stdio mode (so
    the Claude Desktop / Cursor stderr stays readable to humans).
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


def _validate_http_env() -> None:
    """Refuse to boot HTTP mode without ``MCP_SERVER_URL``.

    The hosted transport needs a publicly reachable URL so the RFC 9728
    Protected Resource Metadata and RFC 8414 Authorization Server Metadata
    point at the right host. There is no human-in-the-loop secret to gate
    the flow — authorization is auto-approved and the real authentication
    of every tool call is the caller's own ``X-Pexels-Api-Key`` header.
    """
    if not os.environ.get("MCP_SERVER_URL", "").strip():
        sys.stderr.write(
            "[pexels-mcp] ERROR Missing required env var in streamable-http mode: "
            "MCP_SERVER_URL. Set it to the public HTTPS URL of this service "
            "(e.g. https://pexels-mcp.example.com). It is used as the OAuth "
            "issuer_url and the RFC 9728 resource_server_url.\n"
        )
        sys.exit(2)


def main() -> None:
    """Boot the FastMCP server with the configured transport."""
    transport = _resolve_transport()
    _configure_logging(_resolve_log_format(transport))
    logger = logging.getLogger("pexels_mcp_server")
    env_key_present = bool(os.environ.get("PEXELS_API_KEY", "").strip())
    if transport == "stdio" and not env_key_present:
        logger.warning(
            "PEXELS_API_KEY is not set. Stdio clients must provide a key via "
            "env var; tools will return an auth error until it is set."
        )
    if transport == "streamable-http":
        _validate_http_env()

    # Importing here keeps ``python -m pexels_mcp_server --help`` light and
    # defers the FastMCP/httpx import cost until we actually need it.
    from .server import mcp, oauth_provider

    if transport == "stdio":
        logger.info("Starting pexels-mcp-server on stdio transport")
        mcp.run(transport="stdio")
        return

    # HTTP mode from here on.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))

    if oauth_provider is None:
        # Defensive — _validate_http_env should have aborted already.
        sys.stderr.write("[pexels-mcp] ERROR OAuth provider not initialised.\n")
        sys.exit(2)

    from typing import cast

    import uvicorn

    from .transport import (
        ASGIApp,
        healthz_middleware,
        pexels_key_middleware,
    )

    # FastMCP returns a Starlette app already wired with everything we need:
    # - the OAuth routes (/authorize, /token, /register,
    #   /.well-known/oauth-authorization-server)
    # - the RFC 9728 Protected Resource Metadata
    #   (/.well-known/oauth-protected-resource)
    # - the Bearer validator wrapping /mcp
    # - the /login and /login/callback routes we registered with
    #   ``@mcp.custom_route`` over in ``server.py``
    starlette_app = mcp.streamable_http_app()

    # Wrap with the X-Pexels-Api-Key extractor and the platform healthz
    # short-circuit. The outermost wrap runs first per ASGI semantics.
    app: ASGIApp = cast(ASGIApp, starlette_app)
    app = pexels_key_middleware(app)
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
    # SIGKILL. Sized for Koyeb, which sends SIGTERM and waits ~30s by default
    # before the kill — 25s leaves a 5s buffer for uvicorn's own teardown. On
    # Fly.io the default SIGTERM grace is only 5s, so 25s exceeds the window
    # unless you raise `graceful_shutdown_timeout` in fly.toml; uvicorn just
    # gets killed mid-shutdown otherwise, which is no worse than the old
    # 8s default. A Pexels search round-trip is typically under 1s so 25s is
    # generous; lowering it just risks dropping in-flight requests during a
    # rolling deploy on platforms with a longer SIGTERM window.
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
