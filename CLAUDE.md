# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Pexels MCP server: an async Python Model Context Protocol server exposing nine read-only Pexels REST tools (search/get/preview photos, videos, collections) to MCP-aware AI agents. Two transports: `stdio` (local clients like Claude Desktop) and `streamable-http` (hosted multi-tenant deployment on Koyeb/Fly/Cloud Run).

## Commands

All workflows go through `uv` (Python 3.10+ required):

```bash
uv sync --all-extras                  # install deps + dev extras
uv run ruff check                     # lint (line-length=100, target=py310)
uv run ruff format --check            # formatting
uv run mypy src                       # strict type-check (mypy strict mode)
uv run pytest                         # full test suite
uv run pytest tests/test_client.py    # single file
uv run pytest tests/test_schemas.py::test_search_photos_valid  # single test
uv run pytest -q -k "preview"         # by keyword
```

Run the server locally:

```bash
PEXELS_API_KEY=your-key uv run pexels-mcp-server                # stdio (default)
MCP_AUTH_TOKEN=test123 TRANSPORT=streamable-http HOST=0.0.0.0 \
  uv run pexels-mcp-server                                       # HTTP mode
```

Inspect schemas interactively: `npx @modelcontextprotocol/inspector uv run pexels-mcp-server`.

CI matrix: Python 3.10/3.11/3.12 + Docker build & boot smoke test on every PR.

## Architecture

Five-layer request flow plus two helpers:

```text
__main__.py     transport selection, stderr logging, HTTP middleware wiring
   ↓
server.py       FastMCP server + 9 @mcp.tool functions (input shaping, key resolution, error formatting)
   ↓
schemas.py      Pydantic v2 input models (extra="forbid", host allowlist, locale allowlist)
   ↓
client.py       Async httpx wrapper, one retry on 5xx with jittered backoff, never stores a key
   ↓
formatters.py   Token-lean JSON projections + Markdown summaries + rate_limit envelope

Helpers (off the main path):
previews.py     Concurrent thumbnail fetcher for the visual-pick tool (semaphore-capped)
transport.py    ASGI middleware: /healthz, Bearer auth, X-Pexels-Api-Key extractor
```

### Per-request API key resolution (critical)

The Pexels API key is **never stored in server config**. `server._resolve_api_key()` resolves it per tool call in this order:

1. `X-Pexels-Api-Key` header on the live Starlette request (read via `ctx.request_context.request.headers`) — canonical in HTTP mode. **Must be the first source**: FastMCP spawns the session worker at `initialize` time, so a ContextVar set later by ASGI middleware would be invisible to the worker.
2. `pexels_key_ctx` ContextVar populated by `pexels_key_middleware` (covers `stateless_http=True` request-task isolation).
3. `PEXELS_API_KEY` env var (stdio fallback).

When adding new tools, always go through `_resolve_api_key(ctx)`; do not read env vars directly inside a tool function.

### Stateless HTTP posture

FastMCP is configured with `stateless_http=True, json_response=True`. This means no `Mcp-Session-Id` is allocated, the response is one JSON object (not SSE), and the server scales horizontally without sticky sessions. Do not introduce per-session state in `server.py`.

### Middleware order (HTTP mode only)

Built outside-in in `__main__.main()`:

```text
healthz → bearer_auth → pexels_key → FastMCP streamable_http_app
```

`/healthz` and `/readyz` short-circuit before auth (platform probes don't trigger 401 noise). `bearer_auth_middleware` uses `hmac.compare_digest` and rejects non-matching tokens with 401. Refusal to boot without `MCP_AUTH_TOKEN` is enforced in `__main__` (exit code 2) unless `MCP_ALLOW_UNAUTHED=1`.

DNS rebinding protection is **off by default** (`MCP_ALLOWED_HOSTS` unset → `enable_dns_rebinding_protection=False`); Bearer auth is the gate. Set `MCP_ALLOWED_HOSTS` to a comma-separated allowlist to re-enable.

### Preview tool security model

`pexels_preview_media` is the only tool that fetches arbitrary URLs. Three layers prevent SSRF:

1. **Schema-layer allowlist**: `PreviewMediaParams._check_hosts` rejects anything not on `https://images.pexels.com` before any network I/O. Hosts whitelist is `PEXELS_CDN_HOSTS` in `constants.py`.
2. **No redirect following**: `previews.fetch_thumbnails` builds httpx with `follow_redirects=False` — a CDN redirect would bypass the host check.
3. **Caps**: max 6 URLs per call (`PREVIEW_MAX_COUNT`), 256 KB per thumbnail (`PREVIEW_MAX_BYTES`), 12 concurrent fetches process-wide (`_PREVIEW_SEMAPHORE`).

Never relax these in passing.

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

- Tools that need OAuth (Pexels `My Collections`).
- Re-exports of full Pexels payloads — every field must justify itself.
- New runtime deps beyond `mcp`, `httpx`, `pydantic`, `uvicorn`.
- `# type: ignore` without a comment.
- Tests that hit the real Pexels API in CI (use `pytest-httpx`).

## Test layout

`tests/` mirrors `src/pexels_mcp_server/`: `test_client.py` (HTTP layer with pytest-httpx mocks), `test_schemas.py` (validation), `test_formatters.py` (projection shape), `test_previews.py` (CDN whitelist + ImageContent), `test_transport.py` (ASGI middleware), `test_server_config.py` (FastMCP wiring), `test_logging.py` (JSON formatter).

`pytest.ini_options.filterwarnings = ["error", ...]` — any new DeprecationWarning fails the suite. Pin or migrate.

## Notes

- `mcp` SDK is pinned `>=1.25,<2`. Uses `mcp.server.fastmcp` (the official FastMCP shipped with the SDK, **not** the unrelated PrefectHQ fork).
- Pexels' single-video endpoint is at `/v1/videos/videos/:id` — the repeated `videos` segment is intentional per their docs; preserved in `client.get_video`.
- The Dockerfile pins the `uv` image to a content-addressable digest (Dependabot bumps the `0.7` tag + digest together). Don't drop the digest.
- Conventional commits (`feat`, `fix`, `chore`, `refactor`, `test`, `docs`, `style`, `perf`, `ci`, `build`). English for commits, PR descriptions, branch names.
