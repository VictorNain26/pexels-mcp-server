"""ASGI middleware tests covering the three layers wrapping FastMCP:

- ``healthz_middleware`` short-circuits liveness/readiness probes.
- ``bearer_auth_middleware`` enforces the shared-secret Bearer token.
- ``pexels_key_middleware`` extracts ``X-Pexels-Api-Key`` into a ContextVar.

The middleware is the security perimeter for the hosted HTTP deployment; a
regression here would silently break Bearer auth or leak the Pexels key.
"""

from __future__ import annotations

from typing import Any

import pytest

from pexels_mcp_server.transport import (
    bearer_auth_middleware,
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


async def test_bearer_rejects_missing_token() -> None:
    app = bearer_auth_middleware(_passthrough, "secret")
    recorder = _Recorder()
    await app(_http_scope("/mcp"), _noop_receive, recorder)
    assert recorder.messages[0]["status"] == 401


async def test_bearer_rejects_wrong_token() -> None:
    app = bearer_auth_middleware(_passthrough, "secret")
    recorder = _Recorder()
    headers = [(b"authorization", b"Bearer wrong")]
    await app(_http_scope("/mcp", headers), _noop_receive, recorder)
    assert recorder.messages[0]["status"] == 401


async def test_bearer_rejects_non_bearer_scheme() -> None:
    app = bearer_auth_middleware(_passthrough, "secret")
    recorder = _Recorder()
    headers = [(b"authorization", b"Basic dXNlcjpwYXNz")]
    await app(_http_scope("/mcp", headers), _noop_receive, recorder)
    assert recorder.messages[0]["status"] == 401


async def test_bearer_accepts_correct_token() -> None:
    app = bearer_auth_middleware(_passthrough, "secret")
    recorder = _Recorder()
    headers = [(b"authorization", b"Bearer secret")]
    await app(_http_scope("/mcp", headers), _noop_receive, recorder)
    assert recorder.messages[0]["status"] == 200
    assert recorder.messages[1]["body"] == b"downstream"


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
async def test_bearer_exempts_probe_paths(path: str) -> None:
    app = bearer_auth_middleware(_passthrough, "secret")
    recorder = _Recorder()
    await app(_http_scope(path), _noop_receive, recorder)
    assert recorder.messages[0]["status"] == 200


async def test_bearer_passes_through_non_http_scope() -> None:
    app = bearer_auth_middleware(_passthrough, "secret")
    recorder = _Recorder()
    await app({"type": "lifespan"}, _noop_receive, recorder)
    # Downstream lifespan response is a 200 from _passthrough.
    assert recorder.messages[0]["status"] == 200


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
