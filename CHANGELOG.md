# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Breaking (public-MCP variant)
- The OAuth flow is now **auto-approved**: there is no `/login` page, no shared passcode, no human consent step. Anyone with a Pexels API key can connect any MCP HTTP client. The Bearer token issued by `/token` only proves the client navigated the spec-compliant handshake every MCP HTTP client runs unconditionally; the real authentication of every tool call is the caller's own `X-Pexels-Api-Key` header forwarded to `api.pexels.com`.
- **Removed env var**: `MCP_AUTH_PASSCODE`. Drop it from your hosted deployment.
- **Removed routes**: `GET /login` and `POST /login/callback` (no longer needed).
- **Removed methods** from `PexelsOAuthProvider`: `render_login_page`, `handle_login_callback`, `_validate_and_issue_code`, `_prune_expired_state` (state mapping is no longer used). The constructor signature dropped `passcode=` and now takes `server_url=` only.
- This change aligns the server with the pattern used by other "public" MCPs in the Anthropic Connector Directory — anonymous OAuth with downstream API-key auth on each tool call.

### Breaking
- HTTP authentication is now **OAuth 2.1 + RFC 9728**, served end-to-end by the MCP Python SDK (`AuthSettings` + `OAuthAuthorizationServerProvider` + `ProviderTokenVerifier`). The hand-rolled static-Bearer middleware is gone. Clients that previously sent `Authorization: Bearer <MCP_AUTH_TOKEN>` no longer work — they must speak the standard MCP authorization flow (claude.ai web custom connectors, Claude Desktop remote connectors, Claude Code HTTP, MCP Inspector all handle this natively).
- Env-var rename in HTTP mode:
  - **Removed**: `MCP_AUTH_TOKEN`, `MCP_ALLOW_UNAUTHED`.
  - **Required**: `MCP_SERVER_URL` (public HTTPS URL of the service, used as both OAuth `issuer_url` and RFC 9728 `resource_server_url`) and `MCP_AUTH_PASSCODE` (shared secret typed on the `/login` page during the OAuth flow).
- `streamable-http` mode refuses to boot if either `MCP_SERVER_URL` or `MCP_AUTH_PASSCODE` is unset (exit code 2 with an actionable message). Stdio mode is unchanged.

### Added
- `src/pexels_mcp_server/auth.py` — `PexelsOAuthProvider` implementing the SDK's `OAuthAuthorizationServerProvider` protocol: in-memory client store (DCR per RFC 7591), authorization-code grant with PKCE, audience-bound tokens (RFC 8707 `resource` indicator threaded through code → token), short-lived access tokens (1 h), no refresh tokens (clients re-auth on expiry). Includes a minimal `/login` HTML form that asks the user for the shared passcode.
- `tests/test_auth.py` — 14 unit tests covering register/get client, authorize, login page render, callback success / wrong passcode / unknown state / missing fields, code exchange, token expiry, revocation, refresh-token rejection.
- README rewritten as a single hosted-OAuth deployment guide. Every MCP HTTP client (claude.ai web, Claude Desktop, Claude Code, MCP Inspector) uses the same `/mcp` URL and the same OAuth flow; stdio is documented as a power-user mode for Cursor only. The Koyeb section walks the dashboard and CLI routes end-to-end with the new env vars.

### Changed
- `FastMCP` is instantiated with `auth_server_provider`, `token_verifier`, and `auth=AuthSettings(...)` in HTTP mode; the SDK automatically mounts `/.well-known/oauth-protected-resource` (RFC 9728), `/.well-known/oauth-authorization-server` (RFC 8414), `/authorize`, `/token`, `/register`, and wraps `/mcp` with `RequireAuthMiddleware` that emits the spec-compliant `WWW-Authenticate` header pointing to the Protected Resource Metadata URL. `__main__.py` only appends the custom `/login` and `/login/callback` routes for the human passcode step.
- `transport.py` reduced to two middlewares: `healthz_middleware` and `pexels_key_middleware`. The Bearer validation moved to the SDK.
- `tests/test_transport.py` trimmed to the two surviving middlewares; the obsolete Bearer-related tests were dropped (their replacement lives in `test_auth.py`).
- `.env.example` lists the new HTTP-mode variables (`MCP_SERVER_URL`, `MCP_AUTH_PASSCODE`) and removes the old ones. The file now explicitly names the **one supported topology**: hosted Streamable HTTP with OAuth.
- `CLAUDE.md` reorganised around the six-layer flow (`__main__` → `server` → `auth` → `schemas` → `client` → `formatters`), the OAuth wiring diagram, and the rule "no hand-rolled OAuth — the SDK owns the auth surface".
- `CONTRIBUTING.md` project-layout reflects the new module split (`auth.py`, `transport.py`).
- Earlier `[Unreleased]` cleanup entries (graceful-shutdown 25 s, stateless_http comment refresh, types.py removal, `.env.example` enrichment, Koyeb deployment guide, `CLAUDE.md`/`PRIVACY.md` added to `.dockerignore`) were merged into this release.

### Removed
- `bearer_auth_middleware`, `_extract_bearer`, and the `MCP_AUTH_TOKEN` / `MCP_ALLOW_UNAUTHED` env vars. The static-Bearer model never satisfied the MCP authorization spec (no `WWW-Authenticate`, no RFC 9728 metadata, no Dynamic Client Registration) and broke claude.ai web's custom-connector add flow.
- `pexels_preview_media` tool + `src/pexels_mcp_server/previews.py` + `tests/test_previews.py` + `PreviewMediaParams` schema + the preview-specific constants in `constants.py` (`PEXELS_CDN_HOSTS`, `PREVIEW_MAX_COUNT`, `PREVIEW_MAX_CONCURRENT_FETCHES`, `PREVIEW_FETCH_TIMEOUT_SECONDS`, `PREVIEW_MAX_BYTES`). The tool was not part of the Pexels REST surface — it was a vision-pick convenience we invented on top — and the SSRF surface it created (server-side fetch of caller-supplied URLs) had no equivalent in the official Pexels client libraries. Dropped to keep the tool list 1:1 with the Pexels API.
- `src/pexels_mcp_server/types.py` (107 lines of orphan `TypedDict` mirrors of the Pexels API) — already shipped under [0.6.0]'s `[Unreleased]` entry, kept here for the consolidated release notes.

### Tools added in this release
- `pexels_get_my_collections` — wraps the Pexels `GET /v1/collections` endpoint (the bare root, not `/featured`). Lists the collections owned by the API key holder, same envelope shape as `pexels_list_featured_collections`. No OAuth Pexels required: the standard `Authorization: <api_key>` scheme works (confirmed against the official Pexels API docs). Brings the tool count back to nine and the project's coverage to 1:1 with the Pexels REST surface.

## [0.6.0] - 2026-05-19

### Added
- `PRIVACY.md` documenting what the server processes, what it does not store, and the third-party calls it makes. Required for the Anthropic Claude Connector Directory submission.
- README "Three usage examples" section with concrete agent-side prompts for a hero image, a B-roll video and a visual-pick shortlist. Covers the Connector Directory documentation requirement of ≥3 examples.
- Structured JSON logging in HTTP mode. New `LOG_FORMAT` env var (`text` or `json`); defaults to `json` for `streamable-http` and `text` for `stdio`. No new dependency — uses a stdlib `logging.Formatter` subclass.
- Module-level `asyncio.Semaphore(12)` on the preview thumbnail fetcher. Caps concurrent CDN fetches across all MCP sessions so a burst of `pexels_preview_media` calls cannot saturate the httpx pool.
- CI: new `docker-build` job builds the Dockerfile end-to-end on every PR, smoke-tests both the boot-refusal path (no `MCP_AUTH_TOKEN`) and the boot-success path (with `MCP_ALLOW_UNAUTHED=1`). Catches Dockerfile regressions before Koyeb deploy time. Uses GHA cache for fast feedback.

## [0.5.0] - 2026-05-19

### Added
- `.github/workflows/dependabot-auto-merge.yml`. Auto-merges Dependabot PRs that match the low-risk matrix (any patch update, minor dev-deps, minor GitHub Actions). Runtime-minor and major bumps still wait for a human review with an explanatory comment posted by the workflow. Repo-level `Allow auto-merge` is enabled.

## [0.4.0] - 2026-05-19

### Added
- `GET /readyz` readiness probe alongside `/healthz`. Same 200 response today, exposed separately so platforms can wire each probe to its own path (and so we can grow the readiness check later without affecting liveness).
- ASGI middleware test suite (`tests/test_transport.py`) covering Bearer auth, healthz/readyz short-circuit, and the per-request `X-Pexels-Api-Key` ContextVar lifecycle. Closes the coverage gap flagged in the May 2026 DevOps audit.

### Changed
- `pexels_preview_media` summary block now sanitizes upstream error strings (single line, capped at 80 chars) before they flow into the agent context. Avoids leaking httpx exception detail (TLS cert info, IP, redirect chains) into the model.
- `previews.py` HTTP client now uses `follow_redirects=False`. The URL allowlist runs at the schema layer on the initial host only; a CDN redirect to an arbitrary location would have bypassed it. Pexels CDN does not redirect in normal operation.
- `schemas.SearchPhotosParams.locale` and `SearchVideosParams.locale` now reject values not present in `SUPPORTED_LOCALES` instead of silently passing them to Pexels.
- `schemas.CollectionMediaParams.collection_id` now validates against `^[A-Za-z0-9_-]+$` to prevent path-injection patterns landing in URL paths.
- `client._request` retry backoff is now jittered (base 1.0s × 0.25-0.75 random factor) instead of a fixed 1s. Reduces event-loop stalls during bursty AI sessions when Pexels hiccups.
- `Dockerfile` pins the `uv` builder image to its OCI digest (`@sha256:6292…cc70`) for reproducible builds.
- `bearer_auth_middleware` log line now records only the remote IP (not port). Less per-connection metadata in platform log stores.
- `.github/dependabot.yml`: runtime deps (`mcp`, `httpx`, `pydantic`, `uvicorn`) are now grouped into a single weekly PR for minor/patch updates instead of one PR per package.
- `.github/workflows/publish.yml`: re-enabled the `push: tags/v*` trigger, added a mandatory `test` job dependency that invokes the CI workflow, kept the `pypi` deployment environment for the manual reviewer gate.
- `.github/workflows/ci.yml`: added `workflow_call` so the publish workflow can reuse the CI matrix without duplicating it.

## [0.3.0] - 2026-05-19

### Added
- `pexels_preview_media` tool that fetches Pexels CDN thumbnails and returns them as MCP `ImageContent` so vision-capable agents can pick the best image visually after a search.
- GitHub repo metadata: `dependabot.yml` (weekly grouped updates), `SECURITY.md` (private vulnerability reporting), `CONTRIBUTING.md`, PR template and issue templates.
- CI: concurrency group so superseded runs cancel themselves.
- `py.typed` marker so downstream consumers respect the inline type annotations.
- `.editorconfig` for editor-agnostic indentation rules.
- Per-request Pexels API key via the `X-Pexels-Api-Key` HTTP header. Hosted deployments no longer need (and should not have) a server-wide `PEXELS_API_KEY`; each caller supplies their own key and pays their own quota.
- ASGI middleware `pexels_key_middleware` that extracts the header into a `ContextVar`; tool handlers resolve the effective key per call.
- `stateless_http=True, json_response=True` on the `FastMCP` instance. Streamable HTTP now runs fully stateless: no session IDs, single JSON response per call. This matches the SDK-recommended posture for horizontally scaled hosted deployments and aligns with the MCP draft spec direction (sessions removed).
- `timeout_graceful_shutdown=8` on the uvicorn entry point so in-flight tool calls finish cleanly during Koyeb / Fly rolling deploys.
- `MCP_ALLOW_UNAUTHED=1` escape hatch for local development without a Bearer token.

### Changed
- **Breaking**: `PexelsClient.__init__` no longer takes `api_key`. Every public method now accepts `api_key=` as a required keyword. Stdio callers continue to set `PEXELS_API_KEY` in the environment; the server resolves the env var on every call.
- **Breaking**: `__main__.main` no longer fails fast when `PEXELS_API_KEY` is unset. The server boots and tools return an actionable auth error until a key is supplied via env (stdio) or header (HTTP).
- **Breaking**: `MCP_AUTH_TOKEN` is now mandatory in `streamable-http` mode. The process exits with code 2 if it is unset, unless `MCP_ALLOW_UNAUTHED=1` is also set. Closes a real production gap where an operator could ship an open endpoint and silently burn the fallback `PEXELS_API_KEY`.
- `Dockerfile`: split the `COPY src` step from the lockfile copy so the dependency-only `uv sync` layer is cached independently of source changes. Cuts incremental rebuild time noticeably.

## [0.2.0] - 2026-05-19

### Changed
- **Breaking**: every tool now defaults to `response_format="json"` (was `markdown`). Markdown stays available as an opt-in for human inspection.
- **Breaking**: photo projection trimmed. Dropped `liked`, `photographer_id`, `avg_color`, and 4 of the 6 per-orientation `src` URLs. Renamed `url` → `page_url`, exposed `image_url` (original) and `thumbnail_url` (medium) at the top level.
- **Breaking**: video projection trimmed. Dropped `video_pictures`, `tags`, `avg_color`, `full_res`. Kept only the top 3 files by resolution and added `total_files_available` so the agent knows there is more.
- Migrated video endpoints from the deprecated `/videos/*` to `/v1/videos/*` per the latest Pexels API documentation.
- Rewrote every tool docstring to follow Anthropic's [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) guidance: concrete USE WHEN / DO NOT USE WHEN examples, output-shape teaser inline.
- README repositioned as an MCP-for-agents primer. Install via `uvx --from git+...`; no PyPI dependency for first-class use.

### Fixed
- `SearchPhotosParams.color` now actually validates against the `PhotoColor` enum or a 6-digit hex; previously the enum existed but was bypassed.
- `format_collection_media` no longer renders `None` when a payload omits `id`; uses `_safe` like the rest of the formatters.
- `USER_AGENT` now reads the version from `importlib.metadata` so it stays in sync with `pyproject.toml`.

### Removed
- Redundant `PEXELS_API_KEY` check in the FastMCP lifespan. `PexelsClient.__init__` is the source of truth; the CLI entrypoint exits cleanly before the lifespan ever runs.

## [0.1.0] - 2026-05-18

### Added
- Initial release. Eight read-only tools wrapping the Pexels REST API: `pexels_search_photos`, `pexels_curated_photos`, `pexels_get_photo`, `pexels_search_videos`, `pexels_popular_videos`, `pexels_get_video`, `pexels_list_featured_collections`, `pexels_get_collection_media`.
- Async `httpx` client with retry on 5xx and `X-Ratelimit-*` parsing.
- Pydantic v2 strict input models (`extra="forbid"`).
- Stdio and Streamable HTTP transports.
- CI matrix on Python 3.10, 3.11, 3.12.

[Unreleased]: https://github.com/VictorNain26/pexels-mcp-server/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/VictorNain26/pexels-mcp-server/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/VictorNain26/pexels-mcp-server/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/VictorNain26/pexels-mcp-server/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/VictorNain26/pexels-mcp-server/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/VictorNain26/pexels-mcp-server/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/VictorNain26/pexels-mcp-server/releases/tag/v0.1.0
