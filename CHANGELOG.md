# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added
- README "Koyeb deployment guide" section: step-by-step (dashboard + CLI), explicit env-var table, HTTP `/healthz` probe configuration, public-endpoint smoke test using `MCP-Protocol-Version: 2025-06-18`, and the claude.ai custom-connector wiring. Closes the gap where the previous Koyeb section only listed four bullet points.
- `.env.example` now lists every variable `__main__.py` actually reads: `MCP_AUTH_TOKEN`, `MCP_ALLOWED_HOSTS`, `MCP_ALLOW_UNAUTHED`, `LOG_LEVEL`, `LOG_FORMAT`. Previous version omitted half of them.
- `MCP_ALLOWED_HOSTS={{ KOYEB_PUBLIC_DOMAIN }}` is now recommended in the Koyeb env-var table. Re-enables the Origin/Host validation the MCP 2025-06-18 spec marks as `MUST` for Streamable HTTP servers, without breaking generic platform deployments where the hostname is unknown ahead of time.
- `CLAUDE.md` (Claude Code repo guide) and `PRIVACY.md` added to `.dockerignore` so they never ship in the runtime image.

### Changed
- `uvicorn.timeout_graceful_shutdown` bumped from `8` to `25` seconds. Koyeb (and Fly) send `SIGTERM` and wait ~30 s before `SIGKILL`; the previous 8 s window was tight and risked dropping in-flight Pexels API calls during rolling deploys. 25 s leaves a 5 s buffer for uvicorn's own teardown.
- Reworded the `stateless_http=True` rationale comment in `server.py`. The previous wording claimed the MCP draft was "removing session IDs entirely"; the published 2025-06-18 spec keeps `Mcp-Session-Id` as OPTIONAL. The comment now states accurately that opting out of sessions is the right posture for horizontally scaled deployments.

### Removed
- `src/pexels_mcp_server/types.py` (107 lines of `TypedDict` mirrors of the Pexels API). Zero imports across the codebase — confirmed by grepping every `PhotoDict|VideoDict|CollectionDict|RateLimitDict` reference. The types served as inline documentation only; the real shape of upstream payloads is enforced at the Pydantic input boundary and the lean JSON projections in `formatters.py`. Removing the file also drops the only direct use of `typing_extensions` (still pulled transitively via pydantic). Updated `CONTRIBUTING.md` project layout accordingly.

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
