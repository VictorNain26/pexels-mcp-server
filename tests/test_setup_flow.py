"""End-to-end tests for the ``/setup`` BYOK form.

Drives the real module-level FastMCP via ``httpx.ASGITransport`` so we
exercise the actual routes registered in ``server.py`` (not a synthetic
FastMCP like ``test_server_http.py``). The fixture force-imports
``server`` with HTTP-mode env vars set, then restores the pre-test state
on teardown so other tests keep seeing the unauthenticated default.

What we check:

- GET ``/setup`` without a session id returns 404.
- GET ``/setup`` with an unknown session id returns 404.
- GET ``/setup`` with a valid session id returns 200 + HTML form embedding
  the session id and the form action ``/setup``.
- POST ``/setup`` with a valid session id and a Pexels-rejected key
  re-renders the form with an inline error.
- POST ``/setup`` with a valid session id and a Pexels-accepted key
  302-redirects to the client redirect URI with ``code`` and ``state``
  appended, and the freshly minted access token carries the bound key.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pexels_mcp_server.constants import BASE_URL

_SERVER_URL = "https://test.example.com"


@pytest.fixture
def http_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Re-import ``server`` with HTTP env vars set so OAuth routes register.

    The module is reloaded on teardown with the original env so other test
    files keep seeing the stdio (unauthenticated) instance.
    """
    monkeypatch.setenv("TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_SERVER_URL", _SERVER_URL)

    # Drop any previously cached import so the auth wiring runs fresh.
    for mod in ("pexels_mcp_server.server", "pexels_mcp_server"):
        sys.modules.pop(mod, None)

    server = importlib.import_module("pexels_mcp_server.server")
    yield server

    # Restore: drop again so the next test file gets the default stdio import.
    sys.modules.pop("pexels_mcp_server.server", None)
    sys.modules.pop("pexels_mcp_server", None)


def _redirect_params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


async def _client(app: object) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    # httpx ASGITransport defaults to host "testserver"; the DNS rebinding
    # guard derived from MCP_SERVER_URL accepts "test.example.com" only.
    # Override Host to satisfy the guard.
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test.example.com",
        headers={"Host": "test.example.com"},
    )


# --- GET /setup -----------------------------------------------------------


async def test_setup_get_returns_404_without_session(http_server: Any) -> None:
    app = http_server.mcp.streamable_http_app()
    async with await _client(app) as c:
        response = await c.get("/setup")
    assert response.status_code == 404


async def test_setup_get_returns_404_for_unknown_session(http_server: Any) -> None:
    app = http_server.mcp.streamable_http_app()
    async with await _client(app) as c:
        response = await c.get("/setup?session=does-not-exist")
    assert response.status_code == 404


async def test_setup_get_renders_form_for_valid_session(http_server: Any) -> None:
    # Park a /authorize request manually via the provider.
    from mcp.server.auth.provider import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    provider = http_server.oauth_provider
    client = OAuthClientInformationFull(
        client_id="cl1",
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
    )
    await provider.register_client(client)
    setup_url = await provider.authorize(
        client,
        AuthorizationParams(
            state="abc",
            scopes=["mcp"],
            code_challenge="x",
            redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
            redirect_uri_provided_explicitly=True,
            resource=_SERVER_URL,
        ),
    )
    session_id = _redirect_params(setup_url)["session"][0]

    app = http_server.mcp.streamable_http_app()
    async with await _client(app) as c:
        response = await c.get(f"/setup?session={session_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Pexels API key" in body
    assert f'value="{session_id}"' in body
    assert 'action="/setup"' in body
    # Defensive: error block must be empty on the happy GET path.
    assert 'class="error"' not in body


# --- POST /setup ----------------------------------------------------------


async def _make_session(http_server: Any) -> str:
    """Park a /authorize request and return the session id."""
    from mcp.server.auth.provider import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    provider = http_server.oauth_provider
    client = OAuthClientInformationFull(
        client_id="cl-post",
        redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
    )
    await provider.register_client(client)
    setup_url = await provider.authorize(
        client,
        AuthorizationParams(
            state="state-post",
            scopes=["mcp"],
            code_challenge="ch",
            redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
            redirect_uri_provided_explicitly=True,
            resource=_SERVER_URL,
        ),
    )
    return _redirect_params(setup_url)["session"][0]


async def test_setup_post_redirects_on_valid_key(http_server: Any, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/collections?per_page=1",
        json={"page": 1, "per_page": 1, "total_results": 0, "photos": []},
        headers={"X-Ratelimit-Remaining": "200"},
        match_headers={"Authorization": "good-pexels-key"},
    )
    session_id = await _make_session(http_server)
    app = http_server.mcp.streamable_http_app()
    async with await _client(app) as c:
        response = await c.post(
            "/setup",
            data={"session": session_id, "pexels_key": "good-pexels-key"},
        )
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith("https://claude.ai/api/mcp/auth_callback")
    redirect_params = _redirect_params(location)
    assert redirect_params["state"] == ["state-post"]
    code = redirect_params["code"][0]
    assert code.startswith("mcp_")
    # The key is bound to the code and follows through code → token.
    assert http_server.oauth_provider._code_to_key[code] == "good-pexels-key"


async def test_setup_post_re_renders_with_error_on_invalid_key(
    http_server: Any, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/collections?per_page=1",
        status_code=401,
        json={"error": "unauthorized"},
    )
    session_id = await _make_session(http_server)
    app = http_server.mcp.streamable_http_app()
    async with await _client(app) as c:
        response = await c.post(
            "/setup",
            data={"session": session_id, "pexels_key": "wrong-key"},
        )
    assert response.status_code == 400
    assert "Pexels rejected this key" in response.text
    # Session must survive so the user can retry without re-running OAuth.
    assert http_server.oauth_provider.pending_setup(session_id) is not None


async def test_setup_post_returns_400_when_key_missing(http_server: Any) -> None:
    session_id = await _make_session(http_server)
    app = http_server.mcp.streamable_http_app()
    async with await _client(app) as c:
        response = await c.post("/setup", data={"session": session_id, "pexels_key": ""})
    assert response.status_code == 400
    assert "paste your Pexels API key" in response.text


async def test_setup_post_returns_404_when_session_unknown(http_server: Any) -> None:
    app = http_server.mcp.streamable_http_app()
    async with await _client(app) as c:
        response = await c.post("/setup", data={"session": "ghost", "pexels_key": "anything"})
    assert response.status_code == 404
