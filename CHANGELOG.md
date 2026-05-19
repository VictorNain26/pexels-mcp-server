# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added (Redis-backed OAuth state — 2026-05-19)

OAuth state (DCR clients, access tokens, bound Pexels keys) can now live
in Redis instead of process memory. Opt in by setting two env vars on
the Koyeb service:

- `REDIS_URL` — any Redis endpoint (`redis://` or `rediss://`). Tested
  with [Upstash Redis](https://upstash.com/) free tier (10k cmd/day,
  256 MB, TLS) which is the sweet spot for this server's volume (~1 SET
  per OAuth flow + ~1 GET per tool call).
- `MCP_ENCRYPTION_KEY` — 32-byte url-safe base64 Fernet key. The bound
  Pexels API key is encrypted client-side (AES-128-CBC + HMAC-SHA256)
  before being written to Redis, so a leaked Redis snapshot alone yields
  opaque ciphertext.

With this enabled, users keep their session through a Koyeb rolling
deploy — no more "re-paste your Pexels key on every restart". This
matches the persistence pattern of every production MCP connector.

Architecture: a `TokenStore` Protocol (`src/pexels_mcp_server/storage.py`)
with two implementations (`InMemoryTokenStore` for the historical default
and stdio mode, `RedisTokenStore` for the persistent path). The OAuth
provider takes a `TokenStore` parameter; `server.py::_build_oauth_settings`
selects the backend from the env vars. Short-lived state (5-min auth
codes, 15-min `/setup` sessions, the transient code→key binding) stays
in process memory regardless of backend.

Local dev parity: a new `docker-compose.yml` boots the MCP server +
a local Redis with append-only persistence so the Redis code path is
exercised without an external dependency. See `README.md` for the
full setup.

Tests: 12 new in `tests/test_storage.py` cover both backends (in-memory
caps + eviction, Redis round-trip via `fakeredis`, Fernet encryption-at-
rest verification, graceful handling of `MCP_ENCRYPTION_KEY` rotation,
TTL application). `tests/test_auth.py` updated to use the new store
abstraction. 157 unit tests pass (was 145).

New runtime deps: `redis>=5.0`, `cryptography>=42`. Dev dep:
`fakeredis>=2.21`.

Privacy posture documented in `PRIVACY.md` § 2.b. Operator picks the
backend implicitly via env vars; no behavior change for stdio or for
HTTP-mode deployments that don't set `REDIS_URL`.

### Changed (MCP spec 2025-11-25 alignment + doc sync — 2026-05-19) — **BREAKING**

Production-quality audit pass against the latest MCP spec revision
(2025-11-25). Three buckets:

**Spec compliance.**
- Tools now return a `TypedDict` (`PhotoListResult`, `SinglePhotoResult`,
  `VideoListResult`, `SingleVideoResult`, `CollectionMediaResult`) instead
  of a JSON-encoded string. The SDK auto-generates a concrete
  `outputSchema` (with `properties` and `required` set per shape) and
  populates both `structuredContent` (machine-readable, validated against
  the schema) and a JSON `TextContent` block (backwards compat) — SHOULD
  per MCP spec 2025-11-25 for any tool returning parseable data.
- Tool execution errors now **raise** (Pydantic `ValidationError`
  flattened to `Invalid parameters: <field>: <msg>`; Pexels errors
  propagate as-is). FastMCP marks the `CallToolResult` with
  `isError=true` per SEP-1303 instead of returning the error as a normal
  success result.
- `serverInfo.instructions` filled with a short one-paragraph
  description (new in 2025-11-25).
- Bumped reference spec from 2025-06-18 → 2025-11-25 in all docs and
  HTTPS-guard messages.

**Surface simplification.**
- Dropped the `response_format` parameter on every tool. The
  JSON-only direction (CHANGELOG 2026-05-19 tech-lead pass) is now
  reflected in the input schema: no markdown branch, no `ResponseFormat`
  enum, ~100 lines of markdown formatting code removed from
  `formatters.py`. Saves tokens at conversation init and lets every
  tool advertise a clean structured-output contract.
- `_resolve_api_key` now reads the `X-Pexels-Api-Key` header through a
  single canonical source (the `pexels_key_ctx` ContextVar populated by
  the ASGI middleware) instead of also re-reading the request headers
  directly. Same behaviour, half the code.
- `PEXELS_ATTRIBUTION` constant removed (only used by the deleted
  markdown footer).
- `PexelsOAuthProvider` gains a `max_tracked_tokens` cap (default
  10 000) with FIFO eviction of the oldest 10 % on overflow. Symmetric
  to the existing `max_tracked_clients` cap. Worst-case memory under
  sustained traffic is now bounded; previously a long-running instance
  with constant fresh-OAuth churn would grow the token store
  unbounded until restart.
- LLM-facing docstrings completed on `pexels_get_photo`,
  `pexels_get_video` and `pexels_get_collection_media` (USE WHEN +
  DO NOT USE WHEN, per the CLAUDE.md convention).

**Doc drift fixes.** README, landing page, SUBMIT.md and CONTRIBUTING.md
were still referencing the pre-2026-05-19 surface: 9 tools, `thumbnail_url`,
`rate_limit` envelope, `include_previews`, `min_duration` / `max_duration`,
MCP Apps inline rendering, `pexels_curated_photos` /
`pexels_popular_videos` / `pexels_list_featured_collections` /
`pexels_get_my_collections`, and the wrong "advanced settings → add
header" connect flow (the BYOK `/setup` form has replaced it since
PR #14). All four docs now describe what the code actually does.

### Reverted
- `DEFAULT_PER_PAGE` 5 → 15. The previous flip was based on an unverified
  hypothesis ("marketing briefs ask for 3-5"). Reality: when the user
  precises a number, the LLM passes it through explicitly; when they
  don't, 15 is the long-standing Pexels default and gives the agent more
  candidates to filter from. Token savings vs 5 are marginal (~500
  tokens/call) compared to the other simplifications already in this
  release. Aligns the default with Pexels' own.

### Changed (tech-lead token optimization — 2026-05-19) — **BREAKING**

This pass simplifies the MCP surface to JSON-only output and drops the
inspiration-mode tools nobody used in practice. The goal is minimal
tokens at every layer (tool list at conversation init, tool result per
call, payload per item).

**Tool surface** (9 → 5 tools, ~30 % smaller tool list presented to the LLM):
- **Dropped** `pexels_curated_photos`, `pexels_popular_videos`,
  `pexels_list_featured_collections`, `pexels_get_my_collections` — niche
  discovery endpoints. Most marketing workflows go straight to
  `pexels_search_photos` / `pexels_search_videos` with a brief.
- **Kept** `pexels_search_photos`, `pexels_get_photo`,
  `pexels_search_videos`, `pexels_get_video`, `pexels_get_collection_media`.

**JSON projection** (lean):
- **Dropped** `thumbnail_url` from the photo shape — redundant with
  `image_url` (Pexels accepts `?h=350` on either to get a smaller variant).
- **Dropped** the `rate_limit` block from every envelope. The server
  still logs a warning under 100 remaining; the LLM didn't action it.
- **Dropped** every `ImageContent` / `MCP Apps` / `PreviewFetcher` path.
  The user-visible inline display is now driven entirely by the LLM
  rendering `[alt](image_url)` Markdown in its response, which claude.ai
  renders as clickable image links natively. No server-side base64,
  no images.pexels.com outbound fetches, no UI iframe.
- **Videos**: replaced `preview_image_url` + `files[]` with the single
  `video_url` (top-quality MP4) + `quality`. One actionable URL, ready
  to hand to the user.

**Defaults**:
- `DEFAULT_PER_PAGE` 15 → 5. Most user briefs ask for 3-5 results; 15
  was paying for ~10 entries the agent never showed.
- `include_previews` parameter **dropped** entirely (server no longer
  fetches thumbnails).
- `aspect_ratio_tolerance` parameter dropped — 5 % is hardcoded.
  Removing the knob shaves bytes off the inputSchema sent to the LLM.

**Docstrings** rewritten to ~30 % of previous size. Each tool now has:
USE WHEN (3 lines), DO NOT USE (1 line), filters (1 line), return shape
(1 line), and the Markdown render instruction (1 line). The previous
multi-paragraph "FILTER RECOVERY" / "HOW TO PRESENT RESULTS" sections
were folded into one-liners pointing at `filter_diagnostics`.

**`filter_diagnostics`** is now only emitted when `post_filter_count == 0
and pre_filter_count > 0` (i.e. when the agent can actionably retry).
Saves tokens on the happy path.

**Estimated token savings**:
- Tool list at conversation init: ~6 400 → ~2 500 tokens (-60 %).
- Tool result per call: ~750 → ~120 tokens (-84 %).
- A 4-call conversation: ~7 000 tokens saved cumulative.

**Code cleanup** :
- Deleted `src/pexels_mcp_server/previews.py` (~210 lines).
- Deleted `src/pexels_mcp_server/templates/results_grid.html` (~310 lines).
- Deleted `tests/test_previews.py` (~170 lines).
- Trimmed `tests/test_formatters.py`, `tests/test_server_http.py`,
  `tests/test_live_integration.py` of preview / MCP Apps assertions.
- `formatters.py` halved (~200 lines instead of ~470).
- `server.py` trimmed of preview-fetching helpers, MCP Apps resource
  registration, and 4 tool handlers.
- 145 unit tests + 5 live tests pass; coverage 77.9 %.

### Fixed (context overflow on claude.ai — 2026-05-19)
- **`include_previews` default flipped from `true` to `false`.** Embedding 15 base64 thumbnails on every search call burned ~1300 vision tokens per call; 3-4 calls in a single chat overflowed claude.ai's conversation context with the dreaded "Conversation too long" error. The Markdown image syntax the LLM now uses (per the PR #20 docstring guidance) renders inline in claude.ai natively with zero tokens spent on embedded previews. Vision-pick is still available as an opt-in (`include_previews=true`) for callers that need it on top of Pexels' relevance ranking.

### Added (inline display in chat + filter recovery — 2026-05-19)
- **Tool docstrings now instruct the LLM to render each photo with Markdown image syntax** `![alt](image_url)` so claude.ai (and any Markdown-rendering MCP client) shows the photos directly in the conversation — no MCP Apps iframe required, no detour through pexels.com. Solves the day-1 user complaint "I want to see the images in chat" while MCP Apps inline rendering for custom remote connectors waits for Anthropic to activate it.
- Same pattern for videos: render the `preview_image_url` as inline Markdown image + caption (`duration · resolution · uploader`) + direct download link to `files[0].url`. User sees the preview and can save the MP4 in one click.
- **Filter diagnostics** in every search/list response that applied a post-hoc filter (`aspect_ratio`, `min_width`, `min_height`). The new `filter_diagnostics` block carries:
  - `applied_filters`: dict of what the server actually applied
  - `pre_filter_count`: how many candidates Pexels returned
  - `post_filter_count`: how many survived the filter
  - `suggestion`: actionable retry hint when the filter wiped the page (e.g. "Retry without aspect_ratio — the photo can be cropped to target ratio in post")
- Docstrings on `pexels_search_photos` and `pexels_search_videos` carry a **FILTER RECOVERY** section that tells the LLM exactly when and how to retry: drop `aspect_ratio` first if `post_filter_count == 0 < pre_filter_count`, widen the query if `pre_filter_count == 0`.
- 3 new tests in `tests/test_formatters.py` covering the diagnostics block (present when filter applied, absent otherwise, surfaced in the collection-media envelope too).

### Added (MCP Apps inline rendering — 2026-05-19)
- **MCP Apps support** per the official Jan 2026 specification (stable revision `2026-01-26`). Every search/list tool (`pexels_search_photos`, `pexels_curated_photos`, `pexels_get_photo`, `pexels_search_videos`, `pexels_popular_videos`, `pexels_get_video`, `pexels_get_collection_media`) carries `_meta.ui.resourceUri = "ui://pexels/results"`. MCP Apps-aware hosts (claude.ai web, Claude Desktop, Claude Code, Goose, VS Code GitHub Copilot, Postman, MCPJam) preload the linked UI resource and render it as a sandboxed iframe inline in the conversation when a search/list tool returns — the photos are visible to the user, not just to the model.
- New MCP resource `ui://pexels/results` (MIME `text/html;profile=mcp-app`) serving `src/pexels_mcp_server/templates/results_grid.html`. The bundle implements the full MCP Apps wire protocol: `ui/initialize` request, `ui/notifications/initialized`, listens for `ui/notifications/tool-result`, parses the embedded JSON envelope, renders a responsive thumbnail grid, and reports back via `ui/notifications/size-changed`. Handles photos, videos, and collection media (mixed). DOM-only construction (no `innerHTML`) — XSS-safe against attacker-controlled fields in Pexels payloads.
- 3 new tests in `tests/test_server_http.py` covering (1) the UI resource is registered with the correct MIME type, (2) every search/list tool declares `_meta.ui.resourceUri`, (3) the served HTML contains the spec-mandated handshake methods and never uses `innerHTML`.

### Added (marketing filters + tool discovery — 2026-05-19)
- **`aspect_ratio`** filter on every search/list tool. Accepts `"W:H"` (e.g. `"16:9"`, `"1:1"`, `"9:16"`, `"4:5"`, `"21:9"`) or a positive decimal with explicit dot (`"1.5"`). Matched within `aspect_ratio_tolerance` (default 5 %, configurable 0–50 %). The most-requested marketing filter — Pexels' REST API does not natively expose it.
- **`min_width`** / **`min_height`** filters (post-hoc) on every search/list tool. Pexels' native `size` enum is loose (large/medium/small); the explicit pixel floor is what marketing actually needs (~4000 px for A4 print at 300 DPI, ~1920 px for hero banners, etc.).
- **Server oversample logic**: when any post-hoc filter is set, the server asks Pexels for up to 4× the requested `per_page` (capped at 80) so the filter has enough candidates to keep. Documented behaviour: `count` ≤ `per_page` after filtering — raise `per_page` if a tight filter wipes the page.
- **Live integration tests** (`tests/test_live_integration.py`, marker `live`) that hit the real `api.pexels.com` and `images.pexels.com`. Skipped in CI by default (`addopts = "-m 'not live'"`); run locally with `PEXELS_API_KEY=<key> uv run pytest -m live -v`. Caught a real bug on day one (see *Fixed* below).

### Changed
- **`pexels_search_photos`** / **`pexels_search_videos`** docstrings: stronger discovery signal ("**Prefer this tool over web_search whenever the user asks for…**"), explicit list of marketing-context USE WHEN keywords (fascicule, brochure, leaflet, hero banner, blog post, newsletter, slide deck, social media post, story, carrousel, ad creative, mockup, moodboard). Avoids Claude falling back on `web_search` for stock-photo queries.
- **`pexels_curated_photos`** / **`pexels_popular_videos`** docstrings updated to reference the new post-hoc filters.
- **`parse_aspect_ratio`** rejects bare integers like `"16"` (no dot, no colon) — too likely a half-typed `"16:9"` for safety. Requires explicit `"W:H"` or decimal `"1.5"`.

### Fixed
- **`PexelsClient.validate_key()` now probes `/v1/collections` instead of `/v1/curated`**. Pexels serves `/v1/curated` and `/v1/search` through their Cloudflare CDN, which responds **200 with cached content even for invalid keys** — caught only thanks to the live integration tests. `/v1/collections` (the caller's own collections) actually checks the API key and returns 401 on a bad one. The `/setup` form's "Pexels rejected this key" branch now fires correctly.

### Fixed (null-on-defaulted-field — 2026-05-19)
- Tool calls failed with `Input should be 'markdown' or 'json' [type=enum, input_value=None]` when an MCP client serialized `response_format` (or any other field with a non-None default) as `null` instead of omitting it. claude.ai web does exactly this — every schema field is sent on every tool call, defaults included, with `null` for "use the default". Strict Pydantic rejected on the type mismatch.
- `_StrictModel` now declares a `@model_validator(mode="before")` that normalizes `null` to the field's default for any field whose default isn't already `None`. Required fields and explicitly-nullable fields (e.g. `orientation: Orientation | None = None`) are untouched.
- 5 new tests in `tests/test_schemas.py` covering `response_format=null`, `include_previews=null`, `page=null`/`per_page=null`, the no-op case `orientation=null`, and the negative case `query=null` (required → still fails).

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
