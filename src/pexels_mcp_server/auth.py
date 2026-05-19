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
   from the (code → key) map to the (token → key) map.
6. On every tool call, the server reads the Bearer access token from the
   request, looks up the bound Pexels key, and forwards it to Pexels.
   The X-Pexels-Api-Key header remains a fallback for clients (Claude
   Desktop, Cursor, MCP Inspector) that prefer the per-request pattern.

All state lives in process memory. A restart invalidates every token and
forces clients to walk the flow again on the next call.

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

logger = logging.getLogger("pexels_mcp_server.auth")

# How long a freshly issued authorization code stays valid. The MCP spec
# (and OAuth 2.1) recommend short-lived codes; 5 min matches the SDK example.
_AUTHORIZATION_CODE_TTL_SECONDS = 300

# Access-token lifetime. The bound Pexels key is dropped when the token
# expires, so the user has to re-walk /setup. 30 days is the right floor
# for UX: shorter (e.g. 1 h) means re-pasting the key inside a long
# conversation; longer than that gives no real benefit because the
# in-memory token store is wiped on every Koyeb restart (which happens on
# every deploy and weekly on Dependabot updates anyway), so the effective
# TTL is min(TTL, time-until-restart). Pexels keys are low-value secrets
# (free tier, user-regenerable, no financial / PII access), so the longer
# leak-exposure window of a 30-day token is acceptable for this server.
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
    """In-memory OAuth 2.1 Authorization Server provider with BYOK setup.

    The provider implements every method ``FastMCP``'s ``create_auth_routes``
    will call: client registration (RFC 7591 DCR), authorization code grant
    with PKCE, code-to-token exchange, token loading, and revocation. Refresh
    tokens are intentionally not supported — clients re-auth on expiry, which
    keeps the in-memory store bounded and the implementation small.

    Authorization parks the request in a pending-setup session and redirects
    the user-agent to ``/setup?session=<id>``. Once the user submits their
    Pexels API key the setup handler calls :meth:`complete_setup` to mint
    the authorization code and bind the key to it. The binding then follows
    the code → token transition on the next ``/token`` exchange.
    """

    def __init__(
        self,
        *,
        server_url: str,
        max_tracked_clients: int = 10_000,
        max_tracked_tokens: int = 10_000,
        setup_path: str = "/setup",
    ) -> None:
        if not server_url:
            raise ValueError("server_url is required")
        self._server_url = server_url.rstrip("/")
        self._max_tracked_clients = max_tracked_clients
        self._max_tracked_tokens = max_tracked_tokens
        self._setup_path = setup_path

        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._tokens: dict[str, AccessToken] = {}
        # Pending /setup sessions, keyed by random session id.
        self._pending_setups: dict[str, _PendingSetup] = {}
        # Pexels API key bound to an authorization code (set by /setup,
        # consumed during /token exchange to bind the same key to the
        # resulting access token).
        self._code_to_key: dict[str, str] = {}
        # Pexels API key bound to an access token. Tool handlers look up
        # the key by Bearer token on every request.
        self._token_to_key: dict[str, str] = {}
        # Last time we swept expired codes + tokens. Sweeps run at most once
        # every minute so the cost stays O(N) per minute rather than per call.
        self._last_sweep_at: float = 0.0

    # ------------------------------------------------------------------ DCR

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("No client_id provided")
        # DCR is open (anyone can register), so cap the dict. If we exceed
        # the cap, drop the oldest half (FIFO via insertion order). Dropped
        # clients can re-register transparently on their next /authorize.
        if (
            client_info.client_id not in self._clients
            and len(self._clients) >= self._max_tracked_clients
        ):
            drop_count = self._max_tracked_clients // 2
            for stale_id in list(self._clients.keys())[:drop_count]:
                del self._clients[stale_id]
            logger.warning(
                "OAuth client store hit cap (%d), evicted oldest %d entries",
                self._max_tracked_clients,
                drop_count,
            )
        self._clients[client_info.client_id] = client_info
        logger.info("Registered OAuth client %s", client_info.client_id)

    def _maybe_sweep_expired(self, now: float) -> None:
        """Drop expired authorization codes, access tokens and setup sessions.

        Runs at most once a minute (covers the 5 min code TTL, the 1 h token
        TTL and the 15 min setup-session TTL with plenty of margin). Called
        under no lock — the provider is accessed from a single asyncio event
        loop, so dict mutation during iteration is safe as long as we
        materialize the key list first.
        """
        if now - self._last_sweep_at < 60.0:
            return
        self._last_sweep_at = now
        expired_codes = [code for code, ac in self._auth_codes.items() if ac.expires_at < now]
        for code in expired_codes:
            del self._auth_codes[code]
            self._code_to_key.pop(code, None)
        expired_tokens = [
            token
            for token, at in self._tokens.items()
            if at.expires_at is not None and at.expires_at < now
        ]
        for token in expired_tokens:
            del self._tokens[token]
            self._token_to_key.pop(token, None)
        expired_setups = [sid for sid, p in self._pending_setups.items() if p.expires_at < now]
        for sid in expired_setups:
            del self._pending_setups[sid]
        if expired_codes or expired_tokens or expired_setups:
            logger.info(
                "OAuth sweep: dropped %d expired codes, %d expired tokens, "
                "%d expired setup sessions "
                "(remaining: %d codes, %d tokens, %d sessions)",
                len(expired_codes),
                len(expired_tokens),
                len(expired_setups),
                len(self._auth_codes),
                len(self._tokens),
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

    def pexels_key_for_token(self, access_token: str) -> str | None:
        """Return the Pexels API key bound to a given access token, if any.

        Tool handlers look up the caller's Pexels key by the Bearer token
        on every request. A missing entry means the client either skipped
        the BYOK setup (e.g. Claude Desktop with the per-request header) or
        the token expired — both cases are handled upstream by falling back
        to the ``X-Pexels-Api-Key`` header.
        """
        return self._token_to_key.get(access_token)

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

        # Bound memory under sustained traffic: cap the token store and evict
        # the oldest entries when full. Symmetric to ``register_client``'s cap
        # on ``self._clients``. Any client whose token is evicted re-walks
        # OAuth on the next call — same UX cost as a server restart.
        if len(self._tokens) >= self._max_tracked_tokens:
            drop_count = max(1, self._max_tracked_tokens // 10)
            for stale_token in list(self._tokens.keys())[:drop_count]:
                del self._tokens[stale_token]
                self._token_to_key.pop(stale_token, None)
            logger.warning(
                "OAuth token store hit cap (%d), evicted oldest %d entries",
                self._max_tracked_tokens,
                drop_count,
            )

        access_token_str = f"mcp_{secrets.token_hex(32)}"
        self._tokens[access_token_str] = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + _ACCESS_TOKEN_TTL_SECONDS,
            resource=authorization_code.resource,
        )
        # Move the BYOK binding from the consumed code to the issued token.
        bound_key = self._code_to_key.pop(authorization_code.code, None)
        if bound_key is not None:
            self._token_to_key[access_token_str] = bound_key
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
        access_token = self._tokens.get(token)
        if access_token is None:
            return None
        if access_token.expires_at is not None and access_token.expires_at < now:
            del self._tokens[token]
            self._token_to_key.pop(token, None)
            return None
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._tokens.pop(token.token, None)
            self._token_to_key.pop(token.token, None)
