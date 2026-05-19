# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added (inline previews + vision pick — 2026-05-19)
- **Inline thumbnails on every search/list tool.** `pexels_search_photos`, `pexels_curated_photos`, `pexels_get_photo`, `pexels_search_videos`, `pexels_popular_videos`, `pexels_get_video` and `pexels_get_collection_media` now return a list of MCP content blocks: the JSON envelope as the first `TextContent`, then per-result `TextContent` caption + `ImageContent` (medium thumbnail fetched from `images.pexels.com`). Vision-capable MCP clients render the images inline; an agent can do a true vision-based pick instead of relying on the `alt` text alone. Opt-out per call with `include_previews=false` for bulk / token-sensitive workloads.
- New module `src/pexels_mcp_server/previews.py` implementing `PreviewFetcher`: bounded-concurrency (semaphore 12), per-fetch timeout (5 s), oversize cap (500 KB), MIME-type whitelist (`image/*`), host allowlist (`images.pexels.com` only — SSRF surface), FIFO LRU cache (256 entries, 10 min TTL). Failures are best-effort: a missing or oversize thumbnail degrades to caption-only, the rest of the response still ships.
- Rich-content builders in `formatters.py`: `build_photo_list_rich`, `build_video_list_rich`, `build_single_photo_rich`, `build_single_video_rich`, `build_collection_media_rich`. Each returns `list[TextContent | ImageContent]` with the JSON envelope embedded as the first block so any agent that ignores images still gets the full structured response.
- `include_previews: bool` field on every search/list/get param schema. Default `True` — production UX out of the box. Disable per-call when you want the lightweight JSON-only response.
- `PreviewFetcher` is owned by the FastMCP lifespan (one fetcher per process). Closed cleanly alongside the `PexelsClient` on shutdown.
- `tests/test_previews.py` — 16 unit tests covering happy path, allowlist (5 hostile URL shapes), failure modes (404, network error, oversize, wrong MIME), cache reuse, FIFO eviction, TTL expiry.
- 14 new tests in `tests/test_formatters.py` for the rich-content builders + the preview URL pickers.
- 2 new schema tests covering the `include_previews` default and override.

### Changed
- `PRIVACY.md` lists `images.pexels.com` as an outbound target alongside `api.pexels.com`, with the thumbnail cache lifecycle documented.
- `README.md` tool table mentions the inline preview default.

### Changed (UX — 2026-05-19)
- Access-token TTL bumped from **1 hour** to **30 days** in `auth.py::_ACCESS_TOKEN_TTL_SECONDS`. Re-pasting the Pexels key every hour inside a long conversation was the wrong default: the bound key is dropped when the token expires, so the user had to walk `/setup` again every hour. The threat model for this server (Pexels free tier, user-regenerable keys, no PII / no financial access) makes the longer leak-exposure window acceptable. The in-memory token store is wiped on every Koyeb restart anyway, so the effective TTL is min(30 d, time-until-restart), and Koyeb rolling deploys cap that to about a week in practice.
- README and PRIVACY docs updated to match.

### Added (BYOK setup flow + audit fixes — 2026-05-19)
- **BYOK setup flow** at `GET/POST /setup`. The OAuth `/authorize` handler now parks each request and 302-redirects the user's browser to `/setup?session=<id>` instead of auto-approving. The user pastes their Pexels API key into a short HTML form (`src/pexels_mcp_server/templates/setup.html`); the server validates the key against `api.pexels.com` via the new `PexelsClient.validate_key()` probe; on success the OAuth code is minted with the key bound to it, and `exchange_authorization_code` moves the binding from code → access token. Tool calls resolve the caller's Pexels key by Bearer-token lookup via `PexelsOAuthProvider.pexels_key_for_token()`. This solves the claude.ai-web pain where the previous "send X-Pexels-Api-Key on every call" model required a header that the connector UI does not surface. The header remains accepted as a fallback resolution path for power-user clients (Cursor stdio bridges, scripts).
- `PexelsClient.validate_key(api_key)` — single-probe authentication test against `GET /v1/curated?per_page=1`. Returns True on 200, False on 401/403, raises PexelsAPIError on persistent 5xx so `/setup` can distinguish "bad key" (user-actionable) from "Pexels is down" (retry-actionable).
- `_PendingSetup` dataclass + `_pending_setups` / `_code_to_key` / `_token_to_key` maps on the OAuth provider. Sessions expire after 15 min, codes after 5 min, tokens after 1 h; all three are swept together by the existing once-a-minute reaper.
- HTTPS enforcement on `MCP_SERVER_URL` in `__main__._validate_http_env`: refuses to boot with a plain-`http://` URL unless the host is loopback (`127.0.0.1` / `localhost` / `::1`). Aligns with MCP spec 2025-06-18 §Communication Security.
- CI step: `pip-audit --strict` against the lockfile (exported via `uv export`) on every PR. Fails the build on any disclosed CVE in `mcp`, `httpx`, `pydantic`, `uvicorn` or their transitive deps.
- CI step: `pytest --cov=src/pexels_mcp_server --cov-fail-under=75` so a regression below 75 % coverage fails the build. `__main__.py` is excluded (covered by the docker-build smoke test).
- `tests/test_server_http.py` — end-to-end ASGI tests via `httpx.ASGITransport` validating the spec-mandated 401 + `WWW-Authenticate: Bearer ... resource_metadata="..."` header on unauthed `POST /mcp`, plus the `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server` documents.
- `tests/test_setup_flow.py` — e2e tests for the `/setup` GET (form render, 404 on unknown session) and POST (302 on valid key, 400 with inline error on Pexels-rejected key, 404 on unknown session).

### Changed (BYOK + audit fixes)
- Tool-key resolution order in `server._resolve_api_key`: (1) BYOK-bound key looked up by the request's Bearer access token, (2) `X-Pexels-Api-Key` header, (3) `pexels_key_ctx` ContextVar, (4) `PEXELS_API_KEY` env var in stdio only.
- `README.md` now documents the BYOK flow as the primary connect path and demotes the `X-Pexels-Api-Key` header to fallback. The "no secret to type" wording is gone (there *is* one secret — the user's Pexels key — typed once per access-token lifetime).
- `PRIVACY.md` no longer references the removed `pexels_preview_media` tool, the removed `MCP_AUTH_TOKEN` bearer, or `images.pexels.com`. Describes the BYOK key lifecycle: in-memory only, bound to the access token, dropped on expiry / revocation / restart.
- `.github/SECURITY.md` threat-model section rewritten to match the current code (OAuth 2.1 + BYOK, rate limiter, DNS rebinding); the stale `pexels_preview_media` paragraph is gone.
- `CONTRIBUTING.md` project layout entry for `auth.py` now says "+ /setup BYOK form" instead of "+ /login HTML".
- `__main__.py` docstring mentions `/setup` instead of `/login`.

### Added (public-MCP polish + audit findings)
- `rate_limit_middleware` in `transport.py` — sliding-window per source IP, 60 requests/minute by default. Configurable via `MCP_RATE_LIMIT_PER_MINUTE`. Source IP read from `X-Forwarded-For` **from the right minus N hops** (controlled by `MCP_TRUSTED_PROXY_HOPS`, default 1 = "Koyeb LB only"). Prior implementations read the leftmost entry, which is client-controlled and trivially spoofable; the new logic only trusts the part of the chain a known proxy wrote. Returns `429 Too Many Requests` with a spec-compliant `Retry-After` header (RFC 9110 §15.5.20). `/healthz`, `/readyz`, `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server` are exempt.
- `MCP_TRUSTED_PROXY_HOPS` env var — number of trusted proxies in front of the app. Default `1` (Koyeb's LB). Set to `2` for Cloudflare-in-front-of-Koyeb; set to `0` to ignore `X-Forwarded-For` entirely (no proxy chain at all).
- HTTP/2 on the outbound Pexels client (`client.py`). `httpx.AsyncClient(http2=True, limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=60))`. Pexels advertises h2 over ALPN; one TLS connection is now multiplexed across all concurrent tool calls served by the process. New transitive dep: `httpx[http2]` extra (pulls in `h2`, `hpack`, `hyperframe` — ~80 KB total).
- DNS rebinding protection now auto-on by default when `MCP_SERVER_URL` is set. The hostname is added to `allowed_hosts` automatically; `MCP_ALLOWED_HOSTS` is only needed for additional hosts. Previously the protection defaulted to **off** unless `MCP_ALLOWED_HOSTS` was set explicitly, which left the OAuth + landing routes (outside the Bearer gate) reachable via DNS rebinding attacks.
- OAuth provider sweep — `PexelsOAuthProvider._maybe_sweep_expired` drops expired authorization codes and access tokens once a minute. Bounds memory under bot churn (abandoned authorize flows, expired tokens that never get a `load_access_token` call). Client store also capped at `max_tracked_clients=10_000` with FIFO eviction.
- Window-edge fix in the rate limiter — `hits[0] <= cutoff` (was `<`) so a hit at t=0 expires at exactly t=60 on a 60 s window, not at t=61.
- `Pagination.page` upper-bounded at `le=10_000`. Stops a caller from wasting an outbound HTTP round-trip on a `page=999_999_999`.
- Public landing page at `GET /` (replaces the previous 404 on the root). HTML lives in `src/pexels_mcp_server/templates/landing.html`, loaded once at module import via `importlib.resources`. The MCP endpoint URL is rendered client-side from `window.location.origin` so the HTML stays a fully static asset. Served via `@mcp.custom_route` — the SDK's documented hook for non-MCP Starlette endpoints.
- `SUBMIT.md` — tracks the submission status to the Anthropic Connector Directory ([clau.de/mcp-directory-submission](https://clau.de/mcp-directory-submission)). Pre-filled answers, pre-submission checklist (5 categories), common rejection reasons and how this repo addresses each.

### Removed
- `PEXELS_API_KEY` fallback in HTTP mode (`server.py::_resolve_api_key`). The env var was a silent server-wide fallback: any caller who omitted `X-Pexels-Api-Key` would consume the operator's quota. Now ignored in HTTP mode — callers who forget the header get an actionable "key missing" error instead. Stdio mode is unchanged (the env var remains the way local clients like Claude Desktop and Cursor inject their key).
- The boot-time warning "PEXELS_API_KEY is set on the server process" in `__main__.py` — the variable is no longer read in HTTP mode, so the warning is dead code.

### Changed
- `formatters.py` — `json.dumps(envelope, indent=2, ...)` → `json.dumps(envelope, separators=(",",":"), ...)`. Drops ~30 % of response bytes and ~2-3x CPU on the response serialization path. The agents reading the JSON do not need indentation.
- `CLAUDE.md` rewritten as an operational guide ("how to work in this repo + AI lifecycle baseline May 2026") instead of a functional doc duplicate. Functional context now lives in `README.md`, `PRIVACY.md`, `CONTRIBUTING.md`, `SUBMIT.md`, `CHANGELOG.md` — `CLAUDE.md` links there.


- Public landing page at `GET /` (replaces the previous 404 on the root). Served via `@mcp.custom_route` — the SDK's documented hook for non-MCP Starlette endpoints. The page explains what the server is, the URL to plug into claude.ai, the requirement to supply your own Pexels API key, and links back to the GitHub repo. Outside the OAuth gate so anyone can read it.
- `SUBMIT.md` — tracks the submission status to the Anthropic Connector Directory. Lists the official form URL ([clau.de/mcp-directory-submission](https://clau.de/mcp-directory-submission)), the required checklist (security, docs, privacy, branding), per-field pre-filled answers, common rejection reasons and how this repo addresses each.

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
