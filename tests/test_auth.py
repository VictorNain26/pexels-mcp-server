"""Unit tests for the embedded OAuth Authorization Server provider.

The provider runs in **auto-approve** mode: the ``authorize`` method issues
an authorization code immediately and returns the client redirect URI with
``code`` and ``state`` appended. There is no human consent step — the real
authentication of every tool call is the caller's ``X-Pexels-Api-Key``
header (orthogonal to the OAuth flow). These tests cover every method the
SDK's ``create_auth_routes`` will invoke.

The full HTTP integration (FastMCP wiring, ``WWW-Authenticate``,
``/.well-known`` endpoints) is exercised through ``mcp.streamable_http_app()``
in ``test_server_config.py``; this file stays unit-level so failures point
at the provider implementation rather than the SDK plumbing.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl, AnyUrl

from pexels_mcp_server.auth import MCP_SCOPE, PexelsOAuthProvider

SERVER_URL = "https://pexels-mcp.example.com"
CLIENT_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _make_provider() -> PexelsOAuthProvider:
    return PexelsOAuthProvider(server_url=SERVER_URL)


def _make_client(client_id: str = "client-xyz") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=[AnyUrl(CLIENT_REDIRECT)],
    )


def _make_params(state: str | None = "state-abc") -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=[MCP_SCOPE],
        code_challenge="dummy-challenge",
        redirect_uri=AnyUrl(CLIENT_REDIRECT),
        redirect_uri_provided_explicitly=True,
        resource=SERVER_URL,
    )


def _parse_redirect(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_constructor_rejects_empty_server_url() -> None:
    with pytest.raises(ValueError, match="server_url"):
        PexelsOAuthProvider(server_url="")


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


async def test_authorize_issues_code_and_redirects_to_client() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)

    url = await provider.authorize(client, _make_params(state="s-1"))

    # The redirect goes straight to the client (no /login step).
    assert url.startswith(CLIENT_REDIRECT)
    params = _parse_redirect(url)
    assert params["state"] == ["s-1"]
    code = params["code"][0]
    assert code.startswith("mcp_")
    # The code is recorded for later /token exchange.
    assert code in provider._auth_codes


async def test_authorize_generates_state_when_client_omits_it() -> None:
    provider = _make_provider()
    client = _make_client()
    url = await provider.authorize(client, _make_params(state=None))
    params = _parse_redirect(url)
    assert "state" in params
    assert len(params["state"][0]) >= 16


async def test_authorize_preserves_pkce_and_resource_indicator() -> None:
    """RFC 8707 audience + PKCE challenge must survive into the stored code."""
    provider = _make_provider()
    client = _make_client()
    url = await provider.authorize(client, _make_params(state="s-pkce"))
    code = _parse_redirect(url)["code"][0]
    stored = provider._auth_codes[code]
    assert stored.code_challenge == "dummy-challenge"
    assert stored.resource == SERVER_URL
    assert stored.scopes == [MCP_SCOPE]


async def test_authorization_code_exchange_yields_bearer_token() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    url = await provider.authorize(client, _make_params(state="s-2"))
    code = _parse_redirect(url)["code"][0]

    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    assert auth_code.scopes == [MCP_SCOPE]

    token = await provider.exchange_authorization_code(client, auth_code)
    assert token.token_type == "Bearer"
    assert token.access_token.startswith("mcp_")
    assert token.scope == MCP_SCOPE

    # Code is single-use — second load returns None.
    assert (await provider.load_authorization_code(client, code)) is None


async def test_load_access_token_roundtrip_and_expiry() -> None:
    provider = _make_provider()
    client = _make_client()
    url = await provider.authorize(client, _make_params(state="s-3"))
    code = _parse_redirect(url)["code"][0]
    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    issued = await provider.exchange_authorization_code(client, auth_code)

    loaded = await provider.load_access_token(issued.access_token)
    assert loaded is not None
    assert loaded.client_id == client.client_id
    assert loaded.scopes == [MCP_SCOPE]
    assert loaded.resource == SERVER_URL

    # Expire it manually and confirm the loader drops it.
    expired = provider._tokens[issued.access_token].model_copy(
        update={"expires_at": int(time.time()) - 1}
    )
    provider._tokens[issued.access_token] = expired
    assert (await provider.load_access_token(issued.access_token)) is None


async def test_revoke_token_removes_access_token() -> None:
    provider = _make_provider()
    client = _make_client()
    url = await provider.authorize(client, _make_params(state="s-4"))
    code = _parse_redirect(url)["code"][0]
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


def test_anyhttpurl_rejects_non_http_schemes() -> None:
    """Defensive check: AnyHttpUrl rejects non-http schemes, so a tampered
    redirect_uri could not silently route to file:// or javascript:."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AnyHttpUrl("javascript:alert(1)")
