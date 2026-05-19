"""Embedded OAuth 2.1 Authorization Server for the Pexels MCP server.

Implements ``OAuthAuthorizationServerProvider`` from the MCP Python SDK. The
server is its own Resource Server *and* its own Authorization Server in a
single process: ``FastMCP`` mounts the RS-side metadata (RFC 9728) and the
Bearer validation; this module supplies the AS-side authorization-code +
token issuance flow expected by MCP-aware clients (claude.ai web custom
connector, Claude Desktop, Claude Code, MCP Inspector).

Authentication is gated by a single shared **passcode** (``MCP_AUTH_PASSCODE``
env var). The OAuth ``/authorize`` flow lands on ``/login``; the user types
the passcode; the server issues an authorization code and redirects back to
the client. The client exchanges the code at ``/token`` for a Bearer access
token used on subsequent ``/mcp`` calls.

Storage is in-memory: a process restart invalidates every token and forces
clients to re-auth on the next call. This is acceptable for the single-tenant
deployment posture documented in the README; persistence would require Redis
or Postgres and is out of scope for this server.

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

import hmac
import logging
import secrets
import time
from html import escape

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
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger("pexels_mcp_server.auth")

# How long a freshly issued authorization code stays valid. The MCP spec
# (and OAuth 2.1) recommend short-lived codes; 5 min matches the SDK example.
_AUTHORIZATION_CODE_TTL_SECONDS = 300

# Access-token lifetime. claude.ai re-auths transparently when the token
# expires, so a short window limits exposure if a token leaks while the user
# has the conversation open.
_ACCESS_TOKEN_TTL_SECONDS = 3600

# Scope advertised to clients via /.well-known/oauth-authorization-server.
# A single coarse scope is enough for a read-only server.
MCP_SCOPE = "mcp"


class PexelsOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """In-memory OAuth 2.1 Authorization Server provider gated by a passcode.

    The provider implements every method ``FastMCP``'s ``create_auth_routes``
    will call: client registration (RFC 7591 DCR), authorization code grant
    with PKCE, code-to-token exchange, token loading, and revocation. Refresh
    tokens are intentionally not supported — clients re-auth on expiry, which
    keeps the in-memory store bounded and the implementation small.
    """

    def __init__(self, *, server_url: str, passcode: str) -> None:
        if not server_url:
            raise ValueError("server_url is required")
        if not passcode:
            raise ValueError("passcode is required")

        self._server_url = server_url.rstrip("/")
        self._passcode = passcode

        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._tokens: dict[str, AccessToken] = {}
        # state -> {redirect_uri, code_challenge, ..., client_id, resource}
        self._state_mapping: dict[str, dict[str, str | None]] = {}

    # ------------------------------------------------------------------ DCR

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("No client_id provided")
        self._clients[client_info.client_id] = client_info
        logger.info("Registered OAuth client %s", client_info.client_id)

    # ---------------------------------------------------------- /authorize

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Return the URL the user-agent should be redirected to.

        We send the user to our own ``/login`` page; once they validate the
        passcode there, ``handle_login_callback`` issues the authorization
        code and 302s back to ``params.redirect_uri``.
        """
        state = params.state or secrets.token_hex(16)
        self._state_mapping[state] = {
            "redirect_uri": str(params.redirect_uri),
            "code_challenge": params.code_challenge,
            "redirect_uri_provided_explicitly": str(params.redirect_uri_provided_explicitly),
            "client_id": client.client_id,
            # RFC 8707 — carry the resource indicator end-to-end so the issued
            # access token is audience-bound to this MCP server.
            "resource": params.resource,
        }
        return f"{self._server_url}/login?state={state}&client_id={client.client_id}"

    # ------------------------------------------------------------ /login UI

    async def render_login_page(self, request: Request) -> Response:
        """GET /login — minimal HTML form asking for the shared passcode."""
        state = request.query_params.get("state")
        if not state or state not in self._state_mapping:
            raise HTTPException(400, "Invalid or expired state parameter")

        # ``state`` is echoed back inside an HTML attribute; escape defensively
        # even though it comes from our own mapping (avoids any future regression
        # if state ever comes from user input).
        safe_state = escape(state)
        action = f"{self._server_url}/login/callback"
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Pexels MCP - Sign in</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
            background: #0d0d0d;
            color: #e8e8e8;
            display: flex;
            min-height: 100vh;
            align-items: center;
            justify-content: center;
            margin: 0;
        }}
        .card {{
            background: #1a1a1a;
            border: 1px solid #2a2a2a;
            border-radius: 12px;
            padding: 32px;
            width: 360px;
            box-shadow: 0 12px 24px rgba(0,0,0,0.4);
        }}
        h1 {{ font-size: 18px; margin: 0 0 4px 0; }}
        p {{ font-size: 13px; color: #999; margin: 0 0 24px 0; }}
        label {{ display: block; font-size: 12px; color: #ccc; margin-bottom: 6px; }}
        input {{
            width: 100%;
            padding: 10px 12px;
            background: #0d0d0d;
            border: 1px solid #333;
            border-radius: 6px;
            color: #e8e8e8;
            font-size: 14px;
            box-sizing: border-box;
        }}
        input:focus {{ outline: none; border-color: #4a9eff; }}
        button {{
            width: 100%;
            padding: 10px 12px;
            background: #4a9eff;
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            margin-top: 16px;
        }}
        button:hover {{ background: #3a8eef; }}
    </style>
</head>
<body>
    <form class="card" action="{action}" method="post">
        <h1>Pexels MCP</h1>
        <p>Enter the passcode shared by the server owner to connect this client.</p>
        <input type="hidden" name="state" value="{safe_state}">
        <label for="passcode">Passcode</label>
        <input id="passcode" type="password" name="passcode" autofocus required>
        <button type="submit">Sign in</button>
    </form>
</body>
</html>"""
        return HTMLResponse(content=html)

    async def handle_login_callback(self, request: Request) -> Response:
        """POST /login/callback — validate the passcode and issue a code."""
        form = await request.form()
        passcode = form.get("passcode")
        state = form.get("state")

        if not isinstance(passcode, str) or not isinstance(state, str):
            raise HTTPException(400, "Missing passcode or state")

        redirect_uri = self._validate_and_issue_code(passcode=passcode, state=state)
        return RedirectResponse(url=redirect_uri, status_code=302)

    def _validate_and_issue_code(self, *, passcode: str, state: str) -> str:
        state_data = self._state_mapping.get(state)
        if not state_data:
            raise HTTPException(400, "Invalid or expired state")

        if not hmac.compare_digest(passcode.encode("utf-8"), self._passcode.encode("utf-8")):
            raise HTTPException(401, "Invalid passcode")

        redirect_uri = state_data["redirect_uri"]
        code_challenge = state_data["code_challenge"]
        redirect_uri_provided_explicitly = state_data["redirect_uri_provided_explicitly"] == "True"
        client_id = state_data["client_id"]
        resource = state_data.get("resource")

        # Required values from our own state mapping; refuse to ship a code if
        # any is missing rather than silently issuing a broken token.
        assert redirect_uri is not None
        assert code_challenge is not None
        assert client_id is not None

        new_code = f"mcp_{secrets.token_hex(16)}"
        self._auth_codes[new_code] = AuthorizationCode(
            code=new_code,
            client_id=client_id,
            redirect_uri=AnyHttpUrl(redirect_uri),
            redirect_uri_provided_explicitly=redirect_uri_provided_explicitly,
            expires_at=time.time() + _AUTHORIZATION_CODE_TTL_SECONDS,
            scopes=[MCP_SCOPE],
            code_challenge=code_challenge,
            resource=resource,
        )
        del self._state_mapping[state]
        return construct_redirect_uri(redirect_uri, code=new_code, state=state)

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
        access_token = self._tokens.get(token)
        if access_token is None:
            return None
        if access_token.expires_at is not None and access_token.expires_at < time.time():
            del self._tokens[token]
            return None
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._tokens.pop(token.token, None)
