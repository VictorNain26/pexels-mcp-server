# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Pexels MCP server: an async Python Model Context Protocol server exposing nine read-only Pexels REST tools (search/get/list photos, videos, collections) to MCP-aware AI agents.

**One canonical deployment topology**: hosted Streamable HTTP with OAuth 2.1 + RFC 9728. The same `/mcp` endpoint works with claude.ai web custom connectors, Claude Desktop remote connectors, Claude Code HTTP transport, MCP Inspector, and any future MCP HTTP client. Stdio is still wired in FastMCP (zero marginal cost) for clients that can only speak stdio (Cursor), but it is **not** the supported topology — every doc, deploy guide, and security model targets the hosted HTTP mode.

## Commands

All workflows go through `uv` (Python 3.10+ required):

```bash
uv sync --all-extras                  # install deps + dev extras
uv run ruff check                     # lint (line-length=100, target=py310)
uv run ruff format --check            # formatting
uv run mypy src                       # strict type-check
uv run pytest                         # full test suite
uv run pytest tests/test_auth.py      # single file
uv run pytest tests/test_schemas.py::test_search_photos_valid  # single test
uv run pytest -q -k "oauth"           # by keyword
```

Run the server locally in HTTP mode (parity with prod):

```bash
TRANSPORT=streamable-http \
HOST=127.0.0.1 PORT=8000 \
MCP_SERVER_URL=http://127.0.0.1:8000 \
MCP_AUTH_PASSCODE=devpass \
  uv run pexels-mcp-server
```

Stdio mode (Cursor / power users):

```bash
PEXELS_API_KEY=your-key uv run pexels-mcp-server
```

CI matrix: Python 3.10 / 3.11 / 3.12 + Docker build & boot smoke test on every PR.

## Architecture

Six layers, each with one job. Helpers are mounted as Starlette/ASGI middleware around the FastMCP-built app.

```text
__main__.py     transport selection, stderr logging, OAuth env validation, HTTP middleware wiring
   ↓
server.py       FastMCP server + 9 @mcp.tool functions; OAuth wiring (auth_server_provider, token_verifier, AuthSettings)
   ↓
auth.py         PexelsOAuthProvider (in-memory AS), /login page, passcode validation
   ↓
schemas.py      Pydantic v2 input models (extra="forbid", host allowlist, locale allowlist)
   ↓
client.py       Async httpx wrapper for Pexels REST; one retry on 5xx with jittered backoff
   ↓
formatters.py   Token-lean JSON projections + Markdown summaries + rate_limit envelope

ASGI helpers (off the main path):
transport.py    healthz_middleware (/healthz, /readyz) and pexels_key_middleware (X-Pexels-Api-Key)
```

### OAuth wiring (HTTP mode)

The `FastMCP` constructor gets three things in HTTP mode and **nothing OAuth-related** in stdio:

- `auth_server_provider=PexelsOAuthProvider(...)` — implements the SDK's `OAuthAuthorizationServerProvider` protocol (in-memory clients/codes/tokens).
- `token_verifier=ProviderTokenVerifier(provider)` — SDK helper that delegates to `provider.load_access_token`.
- `auth=AuthSettings(issuer_url=MCP_SERVER_URL, resource_server_url=MCP_SERVER_URL, required_scopes=["mcp"], client_registration_options=ClientRegistrationOptions(enabled=True, ...))`.

`FastMCP.streamable_http_app()` then mounts automatically:

- `/.well-known/oauth-protected-resource` (RFC 9728) — RS metadata.
- `/.well-known/oauth-authorization-server` (RFC 8414) — AS metadata.
- `/authorize`, `/token`, `/register` (RFC 7591 DCR) — AS endpoints.
- `RequireAuthMiddleware` in front of `/mcp` — emits the spec-compliant `WWW-Authenticate: Bearer ... resource_metadata=...` on 401.

`__main__.py` appends two custom routes for the human passcode step:

- `GET /login` → minimal HTML form (rendered by `PexelsOAuthProvider.render_login_page`).
- `POST /login/callback` → validates the passcode and issues the authorization code.

The OAuth flow in one diagram (claude.ai's perspective):

```text
GET  /mcp               -> 401 + WWW-Authenticate (resource_metadata URL)
GET  /.well-known/...   -> AS + RS metadata
POST /register          -> client_id
GET  /authorize?state.. -> 302 -> /login?state=...
GET  /login?state=...   -> HTML form
POST /login/callback    -> 302 -> client redirect_uri?code=...&state=...
POST /token             -> { access_token: "mcp_..." }
POST /mcp + Bearer      -> JSON-RPC OK
```

### Per-request Pexels key resolution

Orthogonal to OAuth. `server._resolve_api_key()` resolves the Pexels API key per tool call in this order:

1. `X-Pexels-Api-Key` header on the live Starlette request (HTTP mode).
2. `pexels_key_ctx` ContextVar populated by `pexels_key_middleware`.
3. `PEXELS_API_KEY` env var (stdio fallback; not recommended in HTTP mode).

### Stateless HTTP posture

`FastMCP(stateless_http=True, json_response=True)`. No `Mcp-Session-Id` is allocated; the response is one JSON object per request. The MCP 2025-06-18 spec keeps session IDs OPTIONAL — opting out is the right posture for horizontally scaled deployments.

### Middleware order (HTTP mode only)

Built outside-in in `__main__.main()`:

```text
healthz -> pexels_key -> FastMCP (which itself wraps /mcp with RequireAuthMiddleware)
```

Healthz and readyz short-circuit before any auth (platform probes don't trigger 401 noise). `pexels_key_middleware` extracts `X-Pexels-Api-Key` into a ContextVar before the tool handler runs. The Bearer token validation and OAuth route mounting are owned by the SDK — do **not** add a hand-rolled bearer middleware here.

DNS rebinding protection is **off by default** (`MCP_ALLOWED_HOSTS` unset → `enable_dns_rebinding_protection=False`); OAuth Bearer validation is the gate. On Koyeb, set `MCP_ALLOWED_HOSTS={{ KOYEB_PUBLIC_DOMAIN }}` to re-enable it on the known public host.

### No outbound URL fetching from tools

Every tool talks only to `api.pexels.com` through `client.py`. There is no tool that takes a URL from the caller and fetches it server-side — that vector was deliberately removed to eliminate any SSRF surface. If a future tool needs to fetch caller-supplied URLs, gate them behind an allowlist enforced at the Pydantic schema layer (not at the network layer) and document the threat model in a section like this one.

## Conventions

### Tool design (LLM-facing)

Follow [Anthropic's "Writing tools for agents"](https://www.anthropic.com/engineering/writing-tools-for-agents). Every tool docstring must contain:

- One-line purpose.
- **USE WHEN:** concrete example queries.
- **DO NOT USE WHEN:** anti-cases (sends caller to the right tool).
- Return-shape teaser (JSON envelope keys).

All tools advertise `ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)`. Defaults live in `_READ_ONLY_ANNOTATIONS` in `server.py`.

### Token-lean payloads

`formatters.py` strips Pexels' verbose fields by design: photo responses drop `liked`, `photographer_id`, `avg_color`, and the six per-orientation `src` URLs (keeping only `image_url` + `thumbnail_url`). Video responses keep only the top 3 files by resolution and report `total_files_available`. **Every field added to a projection must justify itself**: if an agent doesn't need it, don't include it.

### Input validation

All tool inputs go through a Pydantic model in `schemas.py` with `ConfigDict(extra="forbid", str_strip_whitespace=True)`. Errors come back as `Invalid parameters: <field>: <reason>` via `_format_error()` — never a raw exception trace.

### Errors

`PexelsAuthError`, `PexelsRateLimitError`, `PexelsAPIError` are the only exceptions to raise from `client.py`. Their messages are agent-actionable ("Set PEXELS_API_KEY...", "Resets at <ISO>. Reduce request frequency."). Do not catch them inside `client.py`; let them surface to `_format_error()` in `server.py`.

### Adding a tool (CONTRIBUTING.md flow)

1. Strict Pydantic model in `schemas.py`.
2. Method in `client.py` returning `(payload, rate_limit)`.
3. Projection in `formatters.py`.
4. Register in `server.py` with `ToolAnnotations`.
5. Docstring written for an LLM (USE WHEN / DO NOT USE WHEN / return shape).
6. Happy-path + validation test.

### What this project rejects

- Tools that need write access to Pexels' API.
- Re-exports of full Pexels payloads — every field must justify itself.
- Hand-rolled OAuth or hand-rolled bearer middleware. The SDK owns the auth surface; we only customize `/login` for the passcode step.
- New runtime deps beyond `mcp`, `httpx`, `pydantic`, `uvicorn`.
- `# type: ignore` without a comment.
- Tests that hit the real Pexels API in CI (use `pytest-httpx`).

## Test layout

`tests/` mirrors `src/pexels_mcp_server/`:

- `test_client.py` (HTTP layer with pytest-httpx mocks)
- `test_schemas.py` (validation)
- `test_formatters.py` (projection shape)
- `test_transport.py` (healthz + pexels_key ASGI middleware)
- `test_auth.py` (PexelsOAuthProvider unit tests — register, authorize, /login flow, code/token exchange, expiry, revoke)
- `test_server_config.py` (FastMCP wiring smoke tests)
- `test_logging.py` (JSON formatter)

`pytest.ini_options.filterwarnings = ["error", ...]` — any new DeprecationWarning fails the suite. Pin or migrate.

## Notes

- `mcp` SDK is pinned `>=1.25,<2`. Uses `mcp.server.fastmcp` (the official FastMCP shipped with the SDK, **not** the unrelated PrefectHQ fork).
- Pexels' single-video endpoint is at `/v1/videos/videos/:id` — the repeated `videos` segment is intentional per their docs; preserved in `client.get_video`.
- The Dockerfile pins the `uv` image to a content-addressable digest (Dependabot bumps the `0.7` tag + digest together). Don't drop the digest.
- Conventional commits (`feat`, `fix`, `chore`, `refactor`, `test`, `docs`, `style`, `perf`, `ci`, `build`). English for commits, PR descriptions, branch names.
- OAuth token storage is **in-memory**. A Koyeb restart invalidates every token; claude.ai re-auths transparently. Persistence (Redis / Postgres) is out of scope.
