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
    _real_ip,
    _SlidingWindowLimiter,
    healthz_middleware,
    pexels_key_ctx,
    pexels_key_middleware,
    rate_limit_middleware,
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


# ----------------------------------------------------------- rate limiter


def test_real_ip_prefers_x_forwarded_for() -> None:
    scope = _http_scope("/mcp", [(b"x-forwarded-for", b"203.0.113.5, 10.0.0.1")])
    assert _real_ip(scope) == "203.0.113.5"


def test_real_ip_falls_back_to_socket() -> None:
    # No X-Forwarded-For header — we trust the socket peer (local dev).
    assert _real_ip(_http_scope("/mcp")) == "1.2.3.4"


def test_real_ip_returns_unknown_without_xff_or_client() -> None:
    scope = {"type": "http", "path": "/mcp", "headers": [], "client": None}
    assert _real_ip(scope) == "unknown"


def test_limiter_constructor_rejects_invalid_args() -> None:
    with pytest.raises(ValueError, match="max_hits"):
        _SlidingWindowLimiter(0, 60.0)
    with pytest.raises(ValueError, match="window_seconds"):
        _SlidingWindowLimiter(60, 0.0)


async def test_limiter_allows_until_window_full() -> None:
    limiter = _SlidingWindowLimiter(max_hits=3, window_seconds=60.0)
    assert (await limiter.hit("ip", now=0.0)) == (True, 0)
    assert (await limiter.hit("ip", now=10.0)) == (True, 0)
    assert (await limiter.hit("ip", now=20.0)) == (True, 0)
    allowed, retry_after = await limiter.hit("ip", now=30.0)
    assert allowed is False
    # First hit at t=0 expires at t=60, so 30s until reset.
    assert retry_after == 31


async def test_limiter_recovers_after_window_slides() -> None:
    limiter = _SlidingWindowLimiter(max_hits=2, window_seconds=60.0)
    await limiter.hit("ip", now=0.0)
    await limiter.hit("ip", now=10.0)
    # Window slides past the first hit; capacity returns.
    allowed, _ = await limiter.hit("ip", now=61.0)
    assert allowed is True


async def test_limiter_isolates_keys() -> None:
    limiter = _SlidingWindowLimiter(max_hits=1, window_seconds=60.0)
    assert (await limiter.hit("alice", now=0.0))[0] is True
    assert (await limiter.hit("bob", now=0.0))[0] is True
    assert (await limiter.hit("alice", now=0.0))[0] is False
    assert (await limiter.hit("bob", now=0.0))[0] is False


async def test_rate_limit_middleware_passes_under_limit() -> None:
    app = rate_limit_middleware(_passthrough, max_per_minute=3)
    headers = [(b"x-forwarded-for", b"203.0.113.5")]
    recorder = _Recorder()
    for _ in range(3):
        await app(_http_scope("/mcp", headers), _noop_receive, recorder)
    # 3 calls, 2 messages each (start + body) = 6 frames.
    assert len(recorder.messages) == 6
    assert all(m["status"] == 200 for m in recorder.messages if "status" in m)


async def test_rate_limit_middleware_returns_429_with_retry_after() -> None:
    app = rate_limit_middleware(_passthrough, max_per_minute=1)
    headers = [(b"x-forwarded-for", b"203.0.113.5")]
    recorder = _Recorder()
    await app(_http_scope("/mcp", headers), _noop_receive, recorder)
    await app(_http_scope("/mcp", headers), _noop_receive, recorder)
    # 1st call: 200 (start + body). 2nd: 429.
    start_responses = [m for m in recorder.messages if m["type"] == "http.response.start"]
    assert start_responses[1]["status"] == 429
    retry_after_present = any(name == b"retry-after" for name, _ in start_responses[1]["headers"])
    assert retry_after_present


@pytest.mark.parametrize(
    "path",
    [
        "/healthz",
        "/readyz",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    ],
)
async def test_rate_limit_exempts_probes_and_discovery(path: str) -> None:
    app = rate_limit_middleware(_passthrough, max_per_minute=1)
    headers = [(b"x-forwarded-for", b"203.0.113.5")]
    recorder = _Recorder()
    # Hit the exempt path many times — none should ever 429.
    for _ in range(5):
        await app(_http_scope(path, headers), _noop_receive, recorder)
    statuses = [m["status"] for m in recorder.messages if m["type"] == "http.response.start"]
    assert all(s == 200 for s in statuses)


async def test_rate_limit_middleware_skips_non_http_scope() -> None:
    app = rate_limit_middleware(_passthrough, max_per_minute=1)
    recorder = _Recorder()
    await app({"type": "lifespan"}, _noop_receive, recorder)
    assert recorder.messages[0]["status"] == 200
