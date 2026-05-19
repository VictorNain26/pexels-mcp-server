"""Embedded OAuth 2.1 Authorization Server for the Pexels MCP server.

Implements ``OAuthAuthorizationServerProvider`` from the MCP Python SDK. The
server is its own Resource Server **and** its own Authorization Server in a
single process: ``FastMCP`` mounts the RS-side metadata (RFC 9728) and the
Bearer validation; this module supplies the AS-side authorization-code +
token issuance flow expected by MCP-aware clients (claude.ai web custom
connector, Claude Desktop, Claude Code, MCP Inspector).

Auto-approve flow
-----------------

This server is designed to be **publicly usable**: anyone with a Pexels API
key can connect their MCP client. There is no human consent step — the
``/authorize`` endpoint issues an authorization code immediately and
redirects the user-agent back to the calling client. The token returned by
``/token`` is therefore not a user identity; it only proves the client
walked through the OAuth handshake.

The **real** authentication of each call is the caller's own
``X-Pexels-Api-Key`` header, forwarded to ``api.pexels.com``. Without a
valid Pexels key, every tool call returns an actionable auth error from
the upstream API — so the server cannot be abused to consume Pexels quota
on someone else's behalf.

Token storage is in-memory; a process restart invalidates every token and
forces clients to re-auth on the next call (they do this transparently).

References
----------
- MCP spec 2025-06-18, Authorization:
  https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
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

# Access-token lifetime. Clients re-auth transparently when the token expires,
# so a short window limits exposure if a token leaks while the user has the
# conversation open.
_ACCESS_TOKEN_TTL_SECONDS = 3600

# Scope advertised to clients via /.well-known/oauth-authorization-server.
# A single coarse scope is enough for a read-only server.
MCP_SCOPE = "mcp"


class PexelsOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """In-memory OAuth 2.1 Authorization Server provider with auto-approve.

    The provider implements every method ``FastMCP``'s ``create_auth_routes``
    will call: client registration (RFC 7591 DCR), authorization code grant
    with PKCE, code-to-token exchange, token loading, and revocation. Refresh
    tokens are intentionally not supported — clients re-auth on expiry, which
    keeps the in-memory store bounded and the implementation small.

    Authorization is **auto-approved**: any client that walks the OAuth flow
    receives a code without a human consent step. The real auth boundary is
    the caller's ``X-Pexels-Api-Key`` header forwarded to Pexels.
    """

    def __init__(self, *, server_url: str, max_tracked_clients: int = 10_000) -> None:
        if not server_url:
            raise ValueError("server_url is required")
        self._server_url = server_url.rstrip("/")
        self._max_tracked_clients = max_tracked_clients

        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._tokens: dict[str, AccessToken] = {}
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
        """Drop expired authorization codes and access tokens.

        Runs at most once a minute (covers the 5 min code TTL and the 1 h
        token TTL with plenty of margin). Called under no lock — the
        provider is accessed from a single asyncio event loop, so dict
        mutation during iteration is safe as long as we materialize the
        key list first.
        """
        if now - self._last_sweep_at < 60.0:
            return
        self._last_sweep_at = now
        expired_codes = [code for code, ac in self._auth_codes.items() if ac.expires_at < now]
        for code in expired_codes:
            del self._auth_codes[code]
        expired_tokens = [
            token
            for token, at in self._tokens.items()
            if at.expires_at is not None and at.expires_at < now
        ]
        for token in expired_tokens:
            del self._tokens[token]
        if expired_codes or expired_tokens:
            logger.info(
                "OAuth sweep: dropped %d expired codes, %d expired tokens "
                "(remaining: %d codes, %d tokens)",
                len(expired_codes),
                len(expired_tokens),
                len(self._auth_codes),
                len(self._tokens),
            )

    # ---------------------------------------------------------- /authorize

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Auto-approve the OAuth authorization request.

        Generates the authorization code immediately and returns the client's
        ``redirect_uri`` with ``code`` and ``state`` appended. The SDK turns
        this into a 302 so the user-agent never sees a server-side page —
        the flow appears instantaneous to the human.
        """
        self._maybe_sweep_expired(time.time())
        state = params.state or secrets.token_hex(16)
        new_code = f"mcp_{secrets.token_hex(16)}"
        self._auth_codes[new_code] = AuthorizationCode(
            code=new_code,
            client_id=client.client_id,
            redirect_uri=AnyHttpUrl(str(params.redirect_uri)),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            expires_at=time.time() + _AUTHORIZATION_CODE_TTL_SECONDS,
            scopes=[MCP_SCOPE],
            code_challenge=params.code_challenge,
            # RFC 8707 — carry the resource indicator end-to-end so the issued
            # access token is audience-bound to this MCP server.
            resource=params.resource,
        )
        logger.info("Issued authorization code for client %s", client.client_id)
        return construct_redirect_uri(str(params.redirect_uri), code=new_code, state=state)

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
        self._tokens[access_token_str] = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + _ACCESS_TOKEN_TTL_SECONDS,
            resource=authorization_code.resource,
        )
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
            return None
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._tokens.pop(token.token, None)
