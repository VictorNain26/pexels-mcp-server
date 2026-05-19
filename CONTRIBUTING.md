# Contributing

Thanks for the interest. This project is small and opinionated; PRs are welcome as long as they keep the surface tight.

## Development setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/VictorNain26/pexels-mcp-server
cd pexels-mcp-server
uv sync --all-extras
```

Run the full check suite before opening a PR:

```bash
uv run ruff check
uv run ruff format --check
uv run mypy src
uv run pytest
```

CI runs the same on Python 3.10, 3.11, 3.12.

## Project layout

```
src/pexels_mcp_server/
  __init__.py          version
  __main__.py          CLI entry point, transport selection, OAuth env validation, HTTP middleware wiring
  server.py            FastMCP server, tool registration, OAuth wiring (HTTP mode)
  auth.py              OAuth Authorization Server (PexelsOAuthProvider) + /setup BYOK form
  storage.py           TokenStore Protocol + InMemoryTokenStore + RedisTokenStore (Fernet-encrypted Pexels keys)
  client.py            Async httpx client wrapping the Pexels REST API
  schemas.py           Pydantic v2 input models (extra="forbid")
  formatters.py        Token-lean dict projections (returned to FastMCP as structuredContent)
  transport.py         ASGI middleware (healthz/readyz, rate limit, X-Pexels-Api-Key extractor)
  constants.py         BASE_URL, pagination limits, User-Agent
  templates/           landing.html (GET /) + setup.html (GET/POST /setup)
tests/
  test_auth.py             OAuth provider unit tests (register, authorize, complete_setup, exchange, expiry, revoke)
  test_client.py           HTTP client (pytest-httpx)
  test_formatters.py       Lean dict projections + post-hoc filter
  test_live_integration.py Hits the real Pexels API (opt-in via `pytest -m live`)
  test_logging.py          JSON formatter + HTTPS / MCP_SERVER_URL guard
  test_schemas.py          Pydantic validation
  test_server_config.py    FastMCP wiring smoke tests (stateless_http, json_response)
  test_server_http.py      End-to-end OAuth discovery (RFC 9728 + RFC 8414 + WWW-Authenticate) via httpx.ASGITransport
  test_setup_flow.py       End-to-end BYOK /setup form (GET/POST, valid key, invalid key, expired session)
  test_storage.py          TokenStore backends (in-memory + Redis via fakeredis, Fernet encryption-at-rest)
  test_transport.py        ASGI middleware (healthz, rate limit, X-Forwarded-For parsing, pexels_key extractor)
```

## Adding a tool

1. Define a strict Pydantic input model in `schemas.py` (`ConfigDict(extra="forbid")`).
2. Implement the HTTP call in `client.py` returning `(payload, rate_limit)`.
3. Implement a projection in `formatters.py` returning a typed `dict` keeping only high-signal fields.
4. Register the tool in `server.py` with a proper `ToolAnnotations` block. Tools default to read-only / idempotent / open-world. The tool function MUST return a `dict` (FastMCP populates `structuredContent` + serialized text automatically per MCP spec 2025-11-25).
5. Write the docstring **for an LLM caller**, not a human dev. See [Anthropic's guide](https://www.anthropic.com/engineering/writing-tools-for-agents). Each tool needs: a one-line purpose, **USE WHEN** with concrete examples, **DO NOT USE WHEN**, and a return-shape teaser.
6. Errors raise from the tool body (Pydantic `ValidationError`, `Pexels*Error` from `client.py`). FastMCP marks the `CallToolResult` with `isError=true` per SEP-1303. Do NOT catch and return a string — that would silently mark the result as success.
7. Add at least one happy-path test and one validation test.

## What this project will not accept

- Tools whose only purpose is to fetch caller-supplied URLs. Anything that
  takes a URL parameter from the caller is a potential SSRF vector and
  must justify its existence against the threat model.
- A re-export of the entire Pexels response. Every field added to a projection must have a clear agent use case.
- New dependencies without justification. The runtime stack is `mcp`, `httpx`, `pydantic`. Keep it that way.
- `# type: ignore` without a comment explaining why.
- Tests that hit the real Pexels API in CI.

## Commit style

Conventional commits (`feat`, `fix`, `chore`, `refactor`, `docs`, `test`, `ci`, `build`, `perf`). Scope is optional but encouraged for non-trivial PRs.

## Release process

Releases are cut by tagging `vX.Y.Z` on `main` after the changelog is updated. PyPI publishing happens via Trusted Publishing in `.github/workflows/publish.yml`. The workflow is currently dispatch-only; once the PyPI project is created and a Trusted Publisher is registered, uncomment the `on: push: tags` trigger.
