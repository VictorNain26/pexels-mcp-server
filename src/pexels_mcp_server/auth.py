"""Embedded OAuth 2.1 Authorization Server for the Pexels MCP server.

Implements ``OAuthAuthorizationServerProvider`` from the MCP Python SDK. The
server is its own Resource Server **and** its own Authorization Server in a
single process: ``FastMCP`` mounts the RS-side metadata (RFC 9728) and the
Bearer validation; this module supplies the AS-side authorization-code +
token issuance flow expected by MCP-aware clients (claude.ai web custom
connector, Claude Desktop, Claude Code, MCP Inspector).

BYOK setup flow
---------------

The server is multi-tenant: each user supplies their own Pexels API key
during the OAuth flow. The flow has one custom step on top of the
SDK-managed OAuth surface:

1. Client calls ``/authorize`` (spec-mandated entry point).
2. Provider parks the request into a short-lived pending session and
   redirects the user-agent to ``/setup?session=<id>`` instead of the
   client redirect URI.
3. ``/setup`` renders an HTML form asking for a Pexels API key. The user
   pastes their key (free from <https://www.pexels.com/api/>) and submits.
4. The setup handler validates the key against ``api.pexels.com``, then
   calls :meth:`complete_setup` which generates an authorization code,
   binds the Pexels key to that code, and returns the original client
   redirect URI with ``code`` + ``state`` appended.
5. The user-agent follows the redirect, the client exchanges the code for
   an access token via ``/token``, and the bound Pexels key is moved
   from the (code → key) map to the persistent (token → key) store.
6. On every tool call, the server reads the Bearer access token from the
   request, looks up the bound Pexels key, and forwards it to Pexels.
   The X-Pexels-Api-Key header remains a fallback for clients (Claude
   Desktop, Cursor, MCP Inspector) that prefer the per-request pattern.

Persistence
-----------

Long-lived state (DCR clients, access tokens, bound Pexels keys) lives
in a :class:`TokenStore` injected by the caller. The default
:class:`InMemoryTokenStore` keeps the historical "wipe on restart"
behaviour; :class:`RedisTokenStore` (selected automatically when
``REDIS_URL`` is set) makes the same state survive restarts so users
don't have to re-walk OAuth on every redeploy.

Short-lived state (auth codes, pending /setup sessions, the transient
code→key binding) stays in process memory regardless of backend: their
TTLs are 5 / 15 minutes and persisting them buys nothing.

References
----------
- MCP spec 2025-11-25, Authorization:
  https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- SDK reference implementation:
  https://github.com/modelcontextprotocol/python-sdk/tree/main/examples/servers/simple-auth
- RFC 9728 (Protected Resource Metadata) — served by FastMCP itself.
- RFC 8707 (Resource Indicators) — ``resource`` parameter threaded through
  the authorization code and access token.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

from .storage import InMemoryTokenStore, TokenStore

logger = logging.getLogger("pexels_mcp_server.auth")

# How long a freshly issued authorization code stays valid. The MCP spec
# (and OAuth 2.1) recommend short-lived codes; 5 min matches the SDK example.
_AUTHORIZATION_CODE_TTL_SECONDS = 300

# Access-token lifetime. With persistent storage (Redis), this is the true
# expiry: the token survives Koyeb restarts so the user keeps their session.
# Without persistence the effective TTL is min(TTL, time-until-restart) but
# 30 days is still the right ceiling: longer adds leak window for no UX gain.
# Pexels keys are low-value secrets (free tier, user-regenerable, no
# financial / PII access), so 30 days is acceptable.
_ACCESS_TOKEN_TTL_SECONDS = 30 * 24 * 3600

# How long a /setup session stays valid. 15 minutes is generous for a
# human pasting their Pexels key into the form; longer windows just bloat
# the in-memory dict if a user abandons the flow.
_SETUP_SESSION_TTL_SECONDS = 900

# Scope advertised to clients via /.well-known/oauth-authorization-server.
# A single coarse scope is enough for a read-only server.
MCP_SCOPE = "mcp"


@dataclass
class _PendingSetup:
    """Parked /authorize request awaiting the user's Pexels key.

    Recreated on every /authorize call and dropped once the user submits
    the setup form (or after :data:`_SETUP_SESSION_TTL_SECONDS`). The
    fields mirror what :class:`AuthorizationParams` and
    :class:`OAuthClientInformationFull` exposed at request time so we can
    reconstruct the exact ``AuthorizationCode`` later without re-running
    PKCE / redirect-URI / scope checks (the SDK already did them).
    """

    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: list[str]
    resource: str | None
    state: str
    expires_at: float = field(default_factory=lambda: time.time() + _SETUP_SESSION_TTL_SECONDS)


class PexelsOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth 2.1 Authorization Server provider with BYOK setup.

    The provider implements every method ``FastMCP``'s ``create_auth_routes``
    will call: client registration (RFC 7591 DCR), authorization code grant
    with PKCE, code-to-token exchange, token loading, and revocation. Refresh
    tokens are intentionally not supported — clients re-auth on expiry, which
    keeps the wire surface small (one less code path the SDK has to handle).

    Authorization parks the request in a pending-setup session and redirects
    the user-agent to ``/setup?session=<id>``. Once the user submits their
    Pexels API key the setup handler calls :meth:`complete_setup` to mint
    the authorization code and bind the key to it. The binding then follows
    the code → token transition on the next ``/token`` exchange and is
    written through to the :class:`TokenStore` so it survives restarts.
    """

    def __init__(
        self,
        *,
        server_url: str,
        store: TokenStore | None = None,
        setup_path: str = "/setup",
    ) -> None:
        if not server_url:
            raise ValueError("server_url is required")
        self._server_url = server_url.rstrip("/")
        self._setup_path = setup_path
        self._store: TokenStore = store if store is not None else InMemoryTokenStore()

        # Short-lived state stays in process memory regardless of backend.
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._pending_setups: dict[str, _PendingSetup] = {}
        self._code_to_key: dict[str, str] = {}
        # Last time we swept expired codes + setup sessions. Sweeps run at
        # most once a minute so the cost stays bounded.
        self._last_sweep_at: float = 0.0

    # ------------------------------------------------------------------ DCR

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self._store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("No client_id provided")
        await self._store.set_client(client_info)
        logger.info("Registered OAuth client %s", client_info.client_id)

    def _maybe_sweep_expired(self, now: float) -> None:
        """Drop expired authorization codes + setup sessions.

        Access tokens and the bound Pexels keys are not swept here:
        Redis evicts them on TTL, and the in-memory backend lazily drops
        them in :meth:`load_access_token` when the caller next reads
        them. Sweeping every minute would be redundant.
        """
        if now - self._last_sweep_at < 60.0:
            return
        self._last_sweep_at = now
        expired_codes = [code for code, ac in self._auth_codes.items() if ac.expires_at < now]
        for code in expired_codes:
            del self._auth_codes[code]
            self._code_to_key.pop(code, None)
        expired_setups = [sid for sid, p in self._pending_setups.items() if p.expires_at < now]
        for sid in expired_setups:
            del self._pending_setups[sid]
        if expired_codes or expired_setups:
            logger.info(
                "OAuth sweep: dropped %d expired codes, %d expired setup sessions "
                "(remaining: %d codes, %d sessions)",
                len(expired_codes),
                len(expired_setups),
                len(self._auth_codes),
                len(self._pending_setups),
            )

    # ---------------------------------------------------------- /authorize

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Park the request and redirect the user-agent to the BYOK form.

        The SDK has already validated the client, the redirect URI and the
        PKCE challenge by the time we get called. We stash everything we
        need to mint a code later, then redirect the user to ``/setup``.
        :meth:`complete_setup` finishes the flow once the user submits their
        Pexels API key.
        """
        if not client.client_id:
            raise ValueError("No client_id provided")
        self._maybe_sweep_expired(time.time())
        state = params.state or secrets.token_hex(16)
        session_id = secrets.token_urlsafe(24)
        self._pending_setups[session_id] = _PendingSetup(
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            scopes=[MCP_SCOPE],
            resource=params.resource,
            state=state,
        )
        logger.info(
            "Parked OAuth request for client %s into setup session %s",
            client.client_id,
            session_id[:8] + "...",
        )
        return f"{self._server_url}{self._setup_path}?session={session_id}"

    # ----------------------------------------------------- BYOK setup helpers

    def pending_setup(self, session_id: str) -> _PendingSetup | None:
        """Return the parked /authorize request for ``session_id``, or None."""
        self._maybe_sweep_expired(time.time())
        pending = self._pending_setups.get(session_id)
        if pending is None:
            return None
        if pending.expires_at < time.time():
            del self._pending_setups[session_id]
            return None
        return pending

    def complete_setup(self, session_id: str, pexels_api_key: str) -> str:
        """Mint the auth code, bind the Pexels key, return the client redirect.

        Raises :class:`LookupError` if the session is unknown or expired —
        the setup view turns that into a user-facing 'session expired' page.
        Validation of ``pexels_api_key`` against ``api.pexels.com`` is the
        caller's responsibility (it lives in the HTTP path, this module
        stays free of httpx imports for easier unit testing).
        """
        pending = self.pending_setup(session_id)
        if pending is None:
            raise LookupError("Setup session not found or expired.")
        new_code = f"mcp_{secrets.token_hex(16)}"
        self._auth_codes[new_code] = AuthorizationCode(
            code=new_code,
            client_id=pending.client_id,
            redirect_uri=AnyHttpUrl(pending.redirect_uri),
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            expires_at=time.time() + _AUTHORIZATION_CODE_TTL_SECONDS,
            scopes=pending.scopes,
            code_challenge=pending.code_challenge,
            # RFC 8707 — carry the resource indicator end-to-end so the issued
            # access token is audience-bound to this MCP server.
            resource=pending.resource,
        )
        self._code_to_key[new_code] = pexels_api_key
        del self._pending_setups[session_id]
        logger.info(
            "Issued authorization code for client %s (BYOK setup complete)",
            pending.client_id,
        )
        return construct_redirect_uri(pending.redirect_uri, code=new_code, state=pending.state)

    async def pexels_key_for_token(self, access_token: str) -> str | None:
        """Return the Pexels API key bound to a given access token, if any.

        Tool handlers look up the caller's Pexels key by the Bearer token
        on every request. A missing entry means the client either skipped
        the BYOK setup (e.g. Claude Desktop with the per-request header) or
        the token expired — both cases are handled upstream by falling back
        to the ``X-Pexels-Api-Key`` header.
        """
        return await self._store.get_pexels_key(access_token)

    # -------------------------------------------------------------- /token

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        return self._auth_codes.get(authorization_code)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        if authorization_code.code not in self._auth_codes:
            raise ValueError("Invalid authorization code")
        if not client.client_id:
            raise ValueError("No client_id provided")

        access_token_str = f"mcp_{secrets.token_hex(32)}"
        access_token = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + _ACCESS_TOKEN_TTL_SECONDS,
            resource=authorization_code.resource,
        )
        await self._store.set_access_token(access_token, _ACCESS_TOKEN_TTL_SECONDS)
        # Move the BYOK binding from the consumed code to the issued token.
        # The Pexels key is encrypted at rest by the Redis store; the
        # in-memory store keeps it as plain string (process-local).
        bound_key = self._code_to_key.pop(authorization_code.code, None)
        if bound_key is not None:
            await self._store.set_pexels_key(access_token_str, bound_key, _ACCESS_TOKEN_TTL_SECONDS)
        del self._auth_codes[authorization_code.code]

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=_ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(authorization_code.scopes),
        )

    # ----------------------------------------------------- refresh tokens

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        # Refresh tokens are not issued; clients re-auth on access-token expiry.
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        raise NotImplementedError(
            "Refresh tokens are not supported. Clients must re-auth on token expiry."
        )

    # --------------------------------------------------- access-token store

    async def load_access_token(self, token: str) -> AccessToken | None:
        now = time.time()
        self._maybe_sweep_expired(now)
        access_token = await self._store.get_access_token(token)
        if access_token is None:
            return None
        if access_token.expires_at is not None and access_token.expires_at < now:
            # Lazy eviction: the in-memory backend keeps expired tokens
            # until we ask. Redis would already have evicted them on TTL.
            await self._store.delete_access_token(token)
            await self._store.delete_pexels_key(token)
            return None
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            await self._store.delete_access_token(token.token)
            await self._store.delete_pexels_key(token.token)
