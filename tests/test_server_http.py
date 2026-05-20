"""End-to-end ASGI tests for the streamable-HTTP transport.

Drives ``mcp.streamable_http_app()`` through ``httpx.ASGITransport`` to
verify the OAuth surface the MCP 2025-11-25 spec mandates is reachable
from the outside — without booting uvicorn.

What we check:

- An unauthenticated ``POST /mcp`` returns ``401`` with a spec-compliant
  ``WWW-Authenticate`` header whose ``resource_metadata=`` URL points at
  this server's Protected Resource Metadata document.
- ``GET /.well-known/oauth-protected-resource`` (RFC 9728) is reachable
  without auth and lists the issuer in ``authorization_servers``.
- ``GET /.well-known/oauth-authorization-server`` (RFC 8414) is reachable
  without auth and advertises the required endpoint URLs.
- The static landing page is **not** mounted on this synthetic instance
  (it lives on the module-level ``mcp`` in ``server.py``), so we do not
  assert anything about ``GET /`` here.

This is the assertion that was previously only covered by the docker
smoke-test in CI on every PR. Doing it as a unit test means the
``WWW-Authenticate`` regression is caught in milliseconds instead of in
the ~1 min Docker build.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from pexels_mcp_server.auth import MCP_SCOPE, PexelsOAuthProvider

_SERVER_URL = "https://test.example.com"


@pytest.fixture
def auth_enabled_app() -> object:
    """Build a fresh OAuth-protected FastMCP, mirroring the prod wiring.

    No tools are registered; the OAuth/discovery surface is what we test.
    The instance is throwaway so each test gets a clean OAuth provider
    state (no leaked codes or tokens between tests).
    """
    provider = PexelsOAuthProvider(server_url=_SERVER_URL)
    server_url_obj = AnyHttpUrl(_SERVER_URL)
    auth = AuthSettings(
        issuer_url=server_url_obj,
        resource_server_url=server_url_obj,
        required_scopes=[MCP_SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[MCP_SCOPE],
            default_scopes=[MCP_SCOPE],
        ),
    )
    mcp = FastMCP(
        name="pexels-mcp-test",
        stateless_http=True,
        json_response=True,
        auth_server_provider=provider,
        auth=auth,
        transport_security=TransportSecuritySettings(
            # "testserver" is httpx ASGITransport's default Host header.
            # Adding it to the allowed list lets the DNS rebinding guard
            # pass during in-process tests without disabling the guard.
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "test.example.com",
                "test.example.com:*",
                "testserver",
            ],
        ),
    )
    return mcp.streamable_http_app()


async def _client(app: object) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_unauthed_mcp_post_returns_401_with_www_authenticate(
    auth_enabled_app: object,
) -> None:
    async for client in _client(auth_enabled_app):
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
        )
    assert response.status_code == 401
    www_auth = response.headers.get("www-authenticate", "")
    assert www_auth.lower().startswith("bearer"), (
        f"WWW-Authenticate must start with 'Bearer', got: {www_auth!r}"
    )
    # RFC 9728 §5.1: WWW-Authenticate carries resource_metadata=.
    assert "resource_metadata=" in www_auth, (
        f"WWW-Authenticate missing resource_metadata=, got: {www_auth!r}"
    )
    # The URL must point at THIS server's PRM document.
    assert "test.example.com" in www_auth


async def test_protected_resource_metadata_is_reachable(auth_enabled_app: object) -> None:
    async for client in _client(auth_enabled_app):
        response = await client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    body = response.json()
    # RFC 9728 §3.1 mandatory field.
    resource = str(body["resource"]).rstrip("/")
    assert resource == _SERVER_URL
    # The issuer of the AS (same process here) must be listed.
    auth_servers = body.get("authorization_servers", [])
    assert any(_SERVER_URL in str(a) for a in auth_servers), (
        f"authorization_servers missing this server: {auth_servers!r}"
    )


async def test_authorization_server_metadata_is_reachable(auth_enabled_app: object) -> None:
    async for client in _client(auth_enabled_app):
        response = await client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    body = response.json()
    # RFC 8414 §2 mandatory fields for an OAuth 2.1 + PKCE + DCR AS.
    assert str(body["issuer"]).rstrip("/") == _SERVER_URL
    for field in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert field in body, f"AS metadata missing required field: {field}"
        assert _SERVER_URL in str(body[field])
    # PKCE (RFC 7636) must be advertised; S256 is mandatory in OAuth 2.1.
    challenge_methods = body.get("code_challenge_methods_supported") or []
    assert "S256" in challenge_methods


async def test_healthz_and_oauth_metadata_do_not_require_auth(
    auth_enabled_app: object,
) -> None:
    """Both well-known endpoints must be reachable without any token.

    The MCP spec demands clients can fetch discovery metadata *before*
    walking the OAuth flow. If these endpoints sat behind the Bearer
    middleware the whole discovery flow would deadlock.
    """
    async for client in _client(auth_enabled_app):
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
        ):
            response = await client.get(path)
            assert response.status_code == 200, (
                f"{path} must be reachable without auth (got {response.status_code})"
            )


# --- Tool registry sanity check ------------------------------------------


def test_tool_registry_holds_exactly_eight_tools() -> None:
    """Eight read-only tools covering the public Pexels API surface:
    five core (search / by-id / collection) + three discovery feeds
    (curated photos, popular videos, featured collections).
    `pexels_get_my_collections` and `pexels_preview_media` were dropped
    in #29 (no public OAuth scope / SSRF surface respectively)."""
    from pexels_mcp_server import server as module

    tools = module.mcp._tool_manager.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "pexels_search_photos",
        "pexels_get_photo",
        "pexels_search_videos",
        "pexels_get_video",
        "pexels_get_collection_media",
        "pexels_get_curated_photos",
        "pexels_get_popular_videos",
        "pexels_get_featured_collections",
    }


# --- MCP Resources + Prompts primitives ---------------------------------


def test_resource_templates_match_pexels_uri_scheme() -> None:
    """Three URI-template resources mirror the get_* tools so a user
    pasting a pexels.com URL gets the content surfaced without the
    agent invoking a tool. Token cost: ~100 chars per descriptor."""
    from pexels_mcp_server import server as module

    templates = module.mcp._resource_manager._templates
    uris = {t.uri_template for t in templates.values()}
    assert uris == {
        "pexels://photo/{photo_id}",
        "pexels://video/{video_id}",
        "pexels://collection/{collection_id}",
    }


def test_prompts_registry_exposes_marketing_workflows() -> None:
    """Two reusable prompts surfaced in the claude.ai connector menu:
    find_hero_image, find_broll. Each one cuts an agent round-trip on
    parameter clarification. ``find_brand_match`` was dropped because
    ``find_hero_image`` already accepts an optional ``brand_color`` —
    carrying a near-duplicate variant doubled the menu noise."""
    from pexels_mcp_server import server as module

    names = {p.name for p in module.mcp._prompt_manager.list_prompts()}
    assert names == {"find_hero_image", "find_broll"}


async def test_prompt_find_hero_image_renders_actionable_brief() -> None:
    """The rendered prompt must name the tool, the filters, and the
    attribution requirement — otherwise the agent can't act on it."""
    from pexels_mcp_server import server as module

    prompt = module.mcp._prompt_manager.get_prompt("find_hero_image")
    assert prompt is not None
    messages = await prompt.render({"topic": "Paris", "brand_color": "blue"})
    rendered = "\n".join(str(m.content) for m in messages)
    assert "pexels_search_photos" in rendered
    assert "Paris" in rendered
    assert "blue" in rendered
    assert "photographer" in rendered
