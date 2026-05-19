"""ASGI middleware tests for the Streamable HTTP transport.

Covers:

- ``healthz_middleware`` short-circuits ``/healthz`` and ``/readyz`` so
  platform probes never trigger the OAuth challenge on ``/mcp``.
- ``pexels_key_middleware`` extracts the per-request ``X-Pexels-Api-Key``
  header into a ``ContextVar`` so tool handlers pick up the caller's key.

OAuth (Bearer validation, RFC 9728 metadata, ``WWW-Authenticate``) is owned
by the SDK and exercised in ``test_auth.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from pexels_mcp_server.transport import (
    healthz_middleware,
    pexels_key_ctx,
    pexels_key_middleware,
)


async def _passthrough(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"downstream", "more_body": False})


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


def _http_scope(path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "path": path,
        "method": "GET",
        "headers": headers or [],
        "client": ("1.2.3.4", 4242),
    }


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
async def test_healthz_short_circuits_probe_paths(path: str) -> None:
    app = healthz_middleware(_passthrough)
    recorder = _Recorder()
    await app(_http_scope(path), _noop_receive, recorder)
    assert recorder.messages[0]["status"] == 200
    assert recorder.messages[1]["body"] == b"ok"


async def test_healthz_passes_through_other_paths() -> None:
    app = healthz_middleware(_passthrough)
    recorder = _Recorder()
    await app(_http_scope("/mcp"), _noop_receive, recorder)
    assert recorder.messages[1]["body"] == b"downstream"


async def test_pexels_key_middleware_sets_contextvar() -> None:
    captured: dict[str, str | None] = {}

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured["value"] = pexels_key_ctx.get()
        await _passthrough(scope, receive, send)

    app = pexels_key_middleware(inner)
    recorder = _Recorder()
    headers = [(b"x-pexels-api-key", b"user-key-123")]
    await app(_http_scope("/mcp", headers), _noop_receive, recorder)
    assert captured["value"] == "user-key-123"


async def test_pexels_key_middleware_resets_after_request() -> None:
    app = pexels_key_middleware(_passthrough)
    recorder = _Recorder()
    headers = [(b"x-pexels-api-key", b"user-key-xyz")]
    await app(_http_scope("/mcp", headers), _noop_receive, recorder)
    assert pexels_key_ctx.get() is None


async def test_pexels_key_middleware_handles_missing_header() -> None:
    captured: dict[str, str | None] = {}

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured["value"] = pexels_key_ctx.get()
        await _passthrough(scope, receive, send)

    app = pexels_key_middleware(inner)
    recorder = _Recorder()
    await app(_http_scope("/mcp"), _noop_receive, recorder)
    assert captured["value"] is None


async def test_pexels_key_middleware_skips_non_http_scope() -> None:
    app = pexels_key_middleware(_passthrough)
    recorder = _Recorder()
    await app({"type": "lifespan"}, _noop_receive, recorder)
    assert recorder.messages[0]["status"] == 200
