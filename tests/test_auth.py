"""Unit tests for the embedded OAuth Authorization Server provider.

The provider runs in **BYOK setup** mode: the ``authorize`` method parks
the request and redirects the user-agent to ``/setup?session=<id>``.
``complete_setup`` then mints the authorization code with the user's
Pexels API key bound to it. ``exchange_authorization_code`` moves that
binding to the issued access token. These tests cover every method the
SDK's ``create_auth_routes`` will invoke plus the BYOK helpers.

The full HTTP integration (FastMCP wiring, ``WWW-Authenticate``,
``/.well-known`` endpoints, ``/setup`` route handlers) is exercised in
``test_server_http.py``; this file stays unit-level so failures point
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


async def _walk_to_client_redirect(
    provider: PexelsOAuthProvider,
    client: OAuthClientInformationFull,
    *,
    state: str | None = "state-abc",
    pexels_key: str = "test-pexels-key",
) -> str:
    """Drive authorize → complete_setup to obtain the client redirect URL.

    Mirrors what the /setup HTTP handler does in production. Keeps the
    individual tests focused on the assertion they care about rather than
    duplicating the two-step ceremony each time.
    """
    setup_url = await provider.authorize(client, _make_params(state=state))
    session_id = _parse_redirect(setup_url)["session"][0]
    return provider.complete_setup(session_id, pexels_key)


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


async def test_authorize_parks_request_and_redirects_to_setup() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)

    url = await provider.authorize(client, _make_params(state="s-1"))

    # /authorize no longer redirects to the client — it redirects the
    # user-agent to the BYOK setup form first.
    assert url.startswith(f"{SERVER_URL}/setup")
    session_id = _parse_redirect(url)["session"][0]
    assert len(session_id) >= 20
    # The pending session is retrievable and carries the parked params.
    pending = provider.pending_setup(session_id)
    assert pending is not None
    assert pending.client_id == client.client_id
    assert pending.code_challenge == "dummy-challenge"
    assert pending.state == "s-1"


async def test_authorize_generates_state_when_client_omits_it() -> None:
    provider = _make_provider()
    client = _make_client()
    setup_url = await provider.authorize(client, _make_params(state=None))
    session_id = _parse_redirect(setup_url)["session"][0]
    pending = provider.pending_setup(session_id)
    assert pending is not None
    assert len(pending.state) >= 16


async def test_complete_setup_issues_code_and_binds_key() -> None:
    """RFC 8707 audience + PKCE challenge must survive into the stored code,
    and the BYOK key must be bound to the freshly minted code."""
    provider = _make_provider()
    client = _make_client()
    setup_url = await provider.authorize(client, _make_params(state="s-pkce"))
    session_id = _parse_redirect(setup_url)["session"][0]

    redirect = provider.complete_setup(session_id, "my-pexels-key")
    assert redirect.startswith(CLIENT_REDIRECT)
    params = _parse_redirect(redirect)
    assert params["state"] == ["s-pkce"]
    code = params["code"][0]
    assert code.startswith("mcp_")

    stored = provider._auth_codes[code]
    assert stored.code_challenge == "dummy-challenge"
    assert stored.resource == SERVER_URL
    assert stored.scopes == [MCP_SCOPE]
    # Key is bound to the code, waiting for the /token exchange to move it
    # over to the issued access token.
    assert provider._code_to_key[code] == "my-pexels-key"
    # Session is consumed (single-use).
    assert provider.pending_setup(session_id) is None


def test_complete_setup_rejects_unknown_session() -> None:
    provider = _make_provider()
    with pytest.raises(LookupError, match="not found"):
        provider.complete_setup("does-not-exist", "any-key")


async def test_complete_setup_rejects_expired_session() -> None:
    provider = _make_provider()
    client = _make_client()
    setup_url = await provider.authorize(client, _make_params(state="s-exp"))
    session_id = _parse_redirect(setup_url)["session"][0]

    # Force-expire the pending session.
    provider._pending_setups[session_id].expires_at = time.time() - 1
    with pytest.raises(LookupError):
        provider.complete_setup(session_id, "any-key")


async def test_authorization_code_exchange_yields_bearer_token() -> None:
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    redirect = await _walk_to_client_redirect(provider, client, state="s-2")
    code = _parse_redirect(redirect)["code"][0]

    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    assert auth_code.scopes == [MCP_SCOPE]

    token = await provider.exchange_authorization_code(client, auth_code)
    assert token.token_type == "Bearer"
    assert token.access_token.startswith("mcp_")
    assert token.scope == MCP_SCOPE

    # Code is single-use — second load returns None.
    assert (await provider.load_authorization_code(client, code)) is None


async def test_exchange_moves_bound_key_from_code_to_token() -> None:
    """The Pexels key submitted in /setup must follow code → token so tool
    handlers can read it back via pexels_key_for_token()."""
    provider = _make_provider()
    client = _make_client()
    redirect = await _walk_to_client_redirect(
        provider, client, state="s-bind", pexels_key="user-pexels-key-123"
    )
    code = _parse_redirect(redirect)["code"][0]
    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    issued = await provider.exchange_authorization_code(client, auth_code)
    # The code → key mapping is consumed; the token → key one is populated.
    assert code not in provider._code_to_key
    assert await provider.pexels_key_for_token(issued.access_token) == "user-pexels-key-123"


async def test_pexels_key_for_token_returns_none_for_unknown_token() -> None:
    provider = _make_provider()
    assert await provider.pexels_key_for_token("nope") is None


async def test_load_access_token_roundtrip_and_expiry() -> None:
    provider = _make_provider()
    client = _make_client()
    redirect = await _walk_to_client_redirect(provider, client, state="s-3")
    code = _parse_redirect(redirect)["code"][0]
    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    issued = await provider.exchange_authorization_code(client, auth_code)

    loaded = await provider.load_access_token(issued.access_token)
    assert loaded is not None
    assert loaded.client_id == client.client_id
    assert loaded.scopes == [MCP_SCOPE]
    assert loaded.resource == SERVER_URL

    # Expire it manually and confirm the loader drops it *and* the bound key.
    # Reach into the in-memory store to mutate the AccessToken's expires_at
    # without going through a network round-trip.
    from pexels_mcp_server.storage import InMemoryTokenStore

    assert isinstance(provider._store, InMemoryTokenStore)
    in_memory_tokens = provider._store._tokens
    expired = in_memory_tokens[issued.access_token].model_copy(
        update={"expires_at": int(time.time()) - 1}
    )
    in_memory_tokens[issued.access_token] = expired
    assert (await provider.load_access_token(issued.access_token)) is None
    assert await provider.pexels_key_for_token(issued.access_token) is None


async def test_revoke_token_removes_access_token_and_bound_key() -> None:
    provider = _make_provider()
    client = _make_client()
    redirect = await _walk_to_client_redirect(
        provider, client, state="s-4", pexels_key="revoke-me-key"
    )
    code = _parse_redirect(redirect)["code"][0]
    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    issued = await provider.exchange_authorization_code(client, auth_code)
    loaded = await provider.load_access_token(issued.access_token)
    assert loaded is not None
    assert await provider.pexels_key_for_token(issued.access_token) == "revoke-me-key"

    await provider.revoke_token(loaded)
    assert (await provider.load_access_token(issued.access_token)) is None
    assert await provider.pexels_key_for_token(issued.access_token) is None


async def test_token_store_caps_at_max_and_evicts_oldest() -> None:
    """Sustained traffic must not grow ``_tokens`` unbounded.

    Symmetric to ``register_client``'s cap on ``_clients``: when the token
    store hits the configured ceiling, the provider evicts the oldest 10 %
    of entries (FIFO) so memory stays bounded on a Nano deployment under
    a flood of fresh OAuth flows."""
    from pexels_mcp_server.storage import InMemoryTokenStore

    store = InMemoryTokenStore(max_tracked_tokens=10)
    provider = PexelsOAuthProvider(server_url=SERVER_URL, store=store)
    client = _make_client()
    await provider.register_client(client)

    issued_tokens: list[str] = []
    for i in range(11):
        redirect = await _walk_to_client_redirect(
            provider, client, state=f"s-{i}", pexels_key=f"k-{i}"
        )
        code = _parse_redirect(redirect)["code"][0]
        auth_code = await provider.load_authorization_code(client, code)
        assert auth_code is not None
        token = await provider.exchange_authorization_code(client, auth_code)
        issued_tokens.append(token.access_token)

    # Cap is 10; the 11th issuance triggered eviction of the oldest 10 % (1).
    # The oldest issued token is gone; the most recent one is present.
    assert (await provider.load_access_token(issued_tokens[0])) is None
    assert (await provider.load_access_token(issued_tokens[-1])) is not None
    # The bound Pexels key for the evicted token is also gone.
    assert await provider.pexels_key_for_token(issued_tokens[0]) is None


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
