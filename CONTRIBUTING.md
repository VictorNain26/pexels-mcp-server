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
  auth.py              In-process OAuth Authorization Server (PexelsOAuthProvider) + /login HTML
  client.py            Async httpx client wrapping the Pexels REST API
  schemas.py           Pydantic v2 input models (extra="forbid")
  formatters.py        Token-lean JSON projections + Markdown bullets
  previews.py          Thumbnail fetcher for the visual-pick tool
  transport.py         ASGI middleware (healthz, X-Pexels-Api-Key extractor)
  constants.py         BASE_URL, allowed hosts, pagination limits
tests/
  test_client.py       HTTP layer (pytest-httpx)
  test_schemas.py      Pydantic validation
  test_formatters.py   Lean output shape
  test_previews.py     CDN whitelist + ImageContent wrapping
  test_transport.py    ASGI middleware (healthz, pexels_key)
  test_auth.py         OAuth provider unit tests (register, authorize, login, exchange, expiry, revoke)
  test_server_config.py  FastMCP wiring smoke tests
  test_logging.py      JSON formatter
```

## Adding a tool

1. Define a strict Pydantic input model in `schemas.py` (`ConfigDict(extra="forbid")`).
2. Implement the HTTP call in `client.py` returning `(payload, rate_limit)`.
3. Implement a projection in `formatters.py` keeping only high-signal fields.
4. Register the tool in `server.py` with a proper `ToolAnnotations` block. Tools default to read-only / idempotent / open-world.
5. Write the docstring **for an LLM caller**, not a human dev. See [Anthropic's guide](https://www.anthropic.com/engineering/writing-tools-for-agents). Each tool needs: a one-line purpose, **USE WHEN** with concrete examples, **DO NOT USE WHEN**, and a return-shape teaser.
6. Add at least one happy-path test and one validation test.

## What this project will not accept

- Tools that require OAuth (Pexels' `My Collections` endpoint).
- A re-export of the entire Pexels response. Every field added to a projection must have a clear agent use case.
- New dependencies without justification. The runtime stack is `mcp`, `httpx`, `pydantic`. Keep it that way.
- `# type: ignore` without a comment explaining why.
- Tests that hit the real Pexels API in CI.

## Commit style

Conventional commits (`feat`, `fix`, `chore`, `refactor`, `docs`, `test`, `ci`, `build`, `perf`). Scope is optional but encouraged for non-trivial PRs.

## Release process

Releases are cut by tagging `vX.Y.Z` on `main` after the changelog is updated. PyPI publishing happens via Trusted Publishing in `.github/workflows/publish.yml`. The workflow is currently dispatch-only; once the PyPI project is created and a Trusted Publisher is registered, uncomment the `on: push: tags` trigger.
