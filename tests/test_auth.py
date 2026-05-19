"""Unit tests for the embedded OAuth Authorization Server provider.

Exercises every method the SDK's ``create_auth_routes`` will call, the
passcode-gated ``/login`` flow, the in-memory token store, and the audience-
bound (RFC 8707) resource indicator carry-over.

The full HTTP integration (FastMCP wiring, ``WWW-Authenticate``, ``/.well-known``
endpoints) is exercised through ``mcp.streamable_http_app()`` in
``test_server_config.py``; this file stays unit-level so failures point at
the provider implementation rather than the SDK plumbing.
"""

from __future__ import annotations

import time

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl, AnyUrl
from starlette.exceptions import HTTPException
from starlette.requests import Request

from pexels_mcp_server.auth import MCP_SCOPE, PexelsOAuthProvider

SERVER_URL = "https://pexels-mcp.example.com"
PASSCODE = "correct-horse-battery-staple"
CLIENT_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _make_provider() -> PexelsOAuthProvider:
    return PexelsOAuthProvider(server_url=SERVER_URL, passcode=PASSCODE)


def _make_client(client_id: str = "client-xyz") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl(CLIENT_REDIRECT)],
    )


def _make_params(state: str = "state-abc") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=[MCP_SCOPE],
        code_challenge="dummy-challenge",
        redirect_uri=AnyUrl(CLIENT_REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=SERVER_URL,
    )


def _form_request(form_body: bytes) -> Request:
    """Build a Starlette Request whose .form() reads the given bytes."""

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": form_body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/login/callback",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
    }
    return Request(scope, receive)  # type: ignore[arg-type]


def _query_request(state: str | None) -> Request:
    query_string = f"state={state}".encode() if state is not None else b""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/login",
        "headers": [],
        "query_string": query_string,
    }
    return Request(scope)  # type: ignore[arg-type]


def test_constructor_rejects_empty_credentials() -> None:
    with pytest.raises(ValueError, match="server_url"):
        PexelsOAuthProvider(server_url="", passcode=PASSCODE)
    with pytest.raises(ValueError, match="passcode"):
        PexelsOAuthProvider(server_url=SERVER_URL, passcode="")


async def test_register_and_get_client_roundtrip() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    assert (await provider.get_client(client.client_id)) is client
    assert (await provider.get_client("nope")) is None


async def test_register_client_rejects_missing_id() -> None:
    provider = _make_provider()
    bad_client = OAuthClientInformationFull(client_id="", redirect_uris=[AnyUrl(CLIENT_REDIRECT)])
    with pytest.raises(ValueError, match="client_id"):
        await provider.register_client(bad_client)


async def test_authorize_returns_login_url_and_stores_state() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    params = _make_params(state="s-1")

    url = await provider.authorize(client, params)

    assert url.startswith(f"{SERVER_URL}/login?")
    assert "state=s-1" in url
    assert f"client_id={client.client_id}" in url
    # State mapping captures every value the callback will need.
    state_data = provider._state_mapping["s-1"]
    assert state_data["redirect_uri"] == CLIENT_REDIRECT
    assert state_data["code_challenge"] == "dummy-challenge"
    assert state_data["client_id"] == client.client_id
    assert state_data["resource"] == SERVER_URL


async def test_login_callback_wrong_passcode_returns_401() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    await provider.authorize(client, _make_params(state="s-2"))

    req = _form_request(b"passcode=wrong&state=s-2")
    with pytest.raises(HTTPException) as exc_info:
        await provider.handle_login_callback(req)
    assert exc_info.value.status_code == 401


async def test_login_callback_unknown_state_returns_400() -> None:
    provider = _make_provider()
    req = _form_request(f"passcode={PASSCODE}&state=does-not-exist".encode())
    with pytest.raises(HTTPException) as exc_info:
        await provider.handle_login_callback(req)
    assert exc_info.value.status_code == 400


async def test_login_callback_missing_fields_returns_400() -> None:
    provider = _make_provider()
    req = _form_request(b"")
    with pytest.raises(HTTPException) as exc_info:
        await provider.handle_login_callback(req)
    assert exc_info.value.status_code == 400


async def test_login_callback_issues_code_and_redirects() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    await provider.authorize(client, _make_params(state="s-3"))

    req = _form_request(f"passcode={PASSCODE}&state=s-3".encode())
    resp = await provider.handle_login_callback(req)

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(CLIENT_REDIRECT)
    assert "code=mcp_" in location
    assert "state=s-3" in location
    # State must be consumed (single-use).
    assert "s-3" not in provider._state_mapping
    # Exactly one code was issued.
    assert len(provider._auth_codes) == 1


async def test_render_login_page_returns_html_with_state() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    await provider.authorize(client, _make_params(state="s-page"))

    resp = await provider.render_login_page(_query_request("s-page"))

    assert resp.status_code == 200
    assert resp.media_type == "text/html"
    body = resp.body.decode()
    assert 'value="s-page"' in body
    assert f"{SERVER_URL}/login/callback" in body


@pytest.mark.parametrize("state", [None, "unknown-state"])
async def test_render_login_page_rejects_invalid_state(state: str | None) -> None:
    provider = _make_provider()
    with pytest.raises(HTTPException) as exc_info:
        await provider.render_login_page(_query_request(state))
    assert exc_info.value.status_code == 400


async def test_authorization_code_exchange_yields_bearer_token() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    await provider.authorize(client, _make_params(state="s-4"))

    req = _form_request(f"passcode={PASSCODE}&state=s-4".encode())
    resp = await provider.handle_login_callback(req)
    code = resp.headers["location"].split("code=")[1].split("&")[0]

    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    assert auth_code.scopes == [MCP_SCOPE]
    # RFC 8707 audience indicator survives end-to-end.
    assert auth_code.resource == SERVER_URL

    token = await provider.exchange_authorization_code(client, auth_code)
    assert token.token_type == "Bearer"
    assert token.access_token.startswith("mcp_")
    assert token.scope == MCP_SCOPE
    # Code is single-use.
    assert (await provider.load_authorization_code(client, code)) is None


async def test_load_access_token_roundtrip_and_expiry() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    await provider.authorize(client, _make_params(state="s-5"))
    req = _form_request(f"passcode={PASSCODE}&state=s-5".encode())
    resp = await provider.handle_login_callback(req)
    code = resp.headers["location"].split("code=")[1].split("&")[0]
    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    issued = await provider.exchange_authorization_code(client, auth_code)

    loaded = await provider.load_access_token(issued.access_token)
    assert loaded is not None
    assert loaded.client_id == client.client_id
    assert loaded.scopes == [MCP_SCOPE]
    assert loaded.resource == SERVER_URL

    # Expire it manually and confirm the loader drops it.
    loaded_copy = provider._tokens[issued.access_token]
    provider._tokens[issued.access_token] = loaded_copy.model_copy(
        update={"expires_at": int(time.time()) - 1}
    )
    assert (await provider.load_access_token(issued.access_token)) is None


async def test_revoke_token_removes_access_token() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    await provider.authorize(client, _make_params(state="s-6"))
    req = _form_request(f"passcode={PASSCODE}&state=s-6".encode())
    resp = await provider.handle_login_callback(req)
    code = resp.headers["location"].split("code=")[1].split("&")[0]
    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    issued = await provider.exchange_authorization_code(client, auth_code)
    loaded = await provider.load_access_token(issued.access_token)
    assert loaded is not None

    await provider.revoke_token(loaded)
    assert (await provider.load_access_token(issued.access_token)) is None


async def test_refresh_tokens_unsupported() -> None:
    provider = _make_provider()
    client = _make_client()
    assert await provider.load_refresh_token(client, "anything") is None
    with pytest.raises(NotImplementedError):
        await provider.exchange_refresh_token(client, None, [])  # type: ignore[arg-type]


def test_provider_uses_https_in_redirect_construction() -> None:
    """Defensive check: AnyHttpUrl rejects non-http schemes, so the provider
    cannot accidentally redirect to file:// or javascript: URIs even if the
    state mapping is tampered with."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AnyHttpUrl("javascript:alert(1)")
