# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

Cumulative notes for everything landed on `main` since v0.6.0.

### Added

- **3-of-3 MCP primitives**. The server now exposes the full MCP surface:
  - 5 tools (model-controlled): `pexels_search_photos`, `pexels_get_photo`,
    `pexels_search_videos`, `pexels_get_video`, `pexels_get_collection_media`.
  - 3 resources (URI templates per RFC 6570): `pexels://photo/{id}`,
    `pexels://video/{id}`, `pexels://collection/{id}`.
  - 3 prompts (claude.ai connector menu): `find_hero_image`, `find_broll`,
    `find_brand_match`.
- **Redis-backed OAuth state** (opt in via `REDIS_URL` +
  `MCP_ENCRYPTION_KEY`). DCR clients, access tokens and bound Pexels
  keys survive server restart. The bound Pexels key is encrypted at rest
  with Fernet (AES-128-CBC + HMAC-SHA256). Local dev parity via
  `docker-compose.yml`.
- **BYOK `/setup` flow**: the user pastes their Pexels key once on a
  short HTML form during the OAuth handshake; the server validates it
  against `api.pexels.com` and binds it to the issued access token for
  30 days. `X-Pexels-Api-Key` header remains a fallback.
- **Post-hoc search filters** on every search tool: `aspect_ratio`
  (e.g. `"16:9"`, ±5 %), `min_width`, `min_height`. Server oversamples
  Pexels by up to 4× when a filter is set. `filter_diagnostics` block
  emitted only when the filter wipes the page.
- **Hardening**: sliding-window rate limit (60 req/min/IP, tunable);
  DNS rebinding allowlist; trusted-proxy hops; HTTPS guard on
  `MCP_SERVER_URL`; HTTP/2 to Pexels; OAuth `redirect_uri` allowlist
  (https + loopback http only — RFC 7591 §2 phishing mitigation);
  Pexels error body sanitization (control chars stripped, token-shaped
  strings redacted, 200-char cap).
- **Public landing page** at `GET /` (HTML in `templates/landing.html`);
  `/readyz` readiness probe; structured JSON logging in HTTP mode.

### Changed — **BREAKING**

- **MCP spec 2025-11-25 alignment**. Tools return a `TypedDict` —
  the SDK auto-generates `outputSchema` and populates
  `structuredContent`. Tool errors **raise** so FastMCP marks the
  `CallToolResult` with `isError=true` per SEP-1303.
  `serverInfo.instructions` populated.
- **OAuth 2.1 + RFC 9728** served end-to-end by the MCP SDK. The
  hand-rolled static-Bearer middleware is gone. Access-token TTL
  bumped from 1 h to 30 days.
- **Env vars**:
  - Removed: `MCP_AUTH_TOKEN`, `MCP_ALLOW_UNAUTHED`,
    `MCP_AUTH_PASSCODE`, `PEXELS_API_KEY` (HTTP-mode fallback).
  - Required (HTTP mode): `MCP_SERVER_URL`.
  - New optional: `REDIS_URL`, `MCP_ENCRYPTION_KEY`,
    `MCP_ALLOWED_HOSTS`, `MCP_RATE_LIMIT_PER_MINUTE`,
    `MCP_TRUSTED_PROXY_HOPS`, `LOG_FORMAT`.
- **Tool surface trimmed to 5 read-only tools** (was 9). Dropped
  `pexels_curated_photos`, `pexels_popular_videos`,
  `pexels_list_featured_collections`, `pexels_get_my_collections`, and
  the SSRF-prone `pexels_preview_media`.
- **Lean JSON projection**: dropped `thumbnail_url`, `rate_limit`
  envelope block, the 6 per-orientation `src` URLs (kept `image_url`).
  Videos keep `video_url` (top-quality MP4) + `quality` only.
- Dropped tool parameters: `response_format` (JSON-only now),
  `include_previews`, `aspect_ratio_tolerance` (hardcoded 5 %),
  `min_duration`, `max_duration`.

### Performance

- **Tool result no longer duplicated on the wire**. The SDK's default
  shipped the same payload twice: once as `structuredContent` and
  once as indented JSON in `content[]`. A 15-photo search burned
  ~3 100 tokens per call this way. Our `_sdk_patches.py` now emits a
  45-char marker in `content[]` while `structuredContent` carries the
  canonical payload — **−1 500 tokens per tool call** on typical
  searches. Killswitch: `_DROP_DUPLICATE_TEXT_CONTENT = False`.
- **SDK `model_dump` patched** to pass `exclude_unset=True`. Without it,
  optional TypedDict fields leak as `"field": null` in
  `structuredContent` and the strict jsonschema rejects the call with
  `"None is not of type 'object'"` — the original "Output validation
  error" from claude.ai.
- **Tool descriptions trimmed** from ~3 875c → 2 229c total. Type-level
  docstrings (`MediaSize`, `PhotoProjection`, `VideoProjection`,
  `FilterDiagnostics`, all the `*Result` envelopes) removed because
  pydantic surfaces them as `description` in every referencing
  `$defs` — net `tools/list` payload now 15 210c vs 18 016c on main.
- `serverInfo.instructions` reduced to one sentence (attribution
  requirement only — the tool list is already in `tools/list`).

### Removed

- `src/pexels_mcp_server/previews.py`, `types.py`, the MCP Apps
  `ui://pexels/results` resource and `templates/results_grid.html` —
  user-visible inline display is now driven by the LLM rendering
  `![alt](image_url)` Markdown, which claude.ai renders natively.
- `InMemoryTokenStore.expired_token_keys` — dead code, no callers.
- `SUBMIT.md`, `CONTRIBUTING.md` — superseded by `README.md` and
  `CLAUDE.md`.

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
