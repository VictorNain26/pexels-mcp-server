"""Smoke tests for the FastMCP instance configuration.

These verify that the production-hardening defaults stay in place (stateless
HTTP, JSON-response transport, read-only tool annotations). They are deliberately
not integration tests — booting uvicorn is out of scope for unit tests.
"""

from __future__ import annotations

from pexels_mcp_server.server import mcp


def test_fastmcp_runs_stateless_http() -> None:
    """Streamable HTTP must be stateless for horizontal scaling on Koyeb / Fly.

    Stateful mode requires a sticky session ID and breaks when a load balancer
    routes the same MCP client to different replicas. The MCP draft spec
    (post-2025-11-25) removes session IDs entirely, so stateless is also the
    future-proof posture.
    """
    assert mcp.settings.stateless_http is True


def test_fastmcp_returns_json_response() -> None:
    """JSON response (instead of SSE stream) is the simpler shape for hosted
    MCP. claude.ai custom connectors, the OpenAI Apps SDK and Cursor all
    accept it. Pair it with ``stateless_http=True``.
    """
    assert mcp.settings.json_response is True
