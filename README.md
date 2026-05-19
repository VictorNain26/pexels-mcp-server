# pexels-mcp-server

[![CI](https://github.com/VictorNain26/pexels-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/VictorNain26/pexels-mcp-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-1.25%2B-7c3aed.svg)](https://modelcontextprotocol.io/)

A Model Context Protocol (MCP) server that gives AI agents access to free stock photos and videos from [Pexels](https://www.pexels.com/). Plug it into Claude Desktop, Claude Code, Cursor or any MCP-aware agent and the model gains **five read-only tools** to search, browse and resolve Pexels media.

Designed around Anthropic's [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) guidance and the MCP spec 2025-11-25: structured JSON responses (`structuredContent` + auto-serialized text), token-lean payloads (no per-resolution clutter), docstrings written for an LLM caller, agent-actionable error messages with `isError=true` per SEP-1303.

## What the agent can do

| Tool | What it does |
|---|---|
| `pexels_search_photos` | Find photos by query, with optional `orientation` / `size` / `color` / `locale` / `min_width` / `min_height` / `aspect_ratio` filters. |
| `pexels_get_photo` | Resolve a photo id to its canonical record. |
| `pexels_search_videos` | Find videos by query, with `orientation` / `size` / `locale` / `min_width` / `min_height` / `aspect_ratio` filters. |
| `pexels_get_video` | Resolve a video id to its canonical record. |
| `pexels_get_collection_media` | Read the photos + videos inside a Pexels collection. |

Every tool returns a **structured JSON envelope** (`page`, `per_page`, `count`, `has_more`, `next_page?`, `total_results?`, per-item metadata). Because the tool function returns a `dict`, the SDK auto-populates both `structuredContent` (for clients that consume it directly) and a serialized JSON `TextContent` block (for backwards compat). When a post-hoc filter wipes the page, the envelope carries a `filter_diagnostics` block telling the agent how to retry.

## How the agent picks the best image

Pexels already ranks search results by relevance. On top of that, the tools are shaped to let the agent reason its way to the right shot in two steps:

1. **Frame the query and filters.** Translate the user's request into a tight search term plus the filters that matter: `orientation` for hero banners, `color` for brand fit, `size` if the user wants print-quality, `aspect_ratio` for fixed-frame placement (Instagram 1:1, Story 9:16, hero 16:9, LinkedIn 4:5), `min_width` / `min_height` for hard pixel floors (~4000 for A4 print, ~1920 for hero). Aggressive filtering up front is cheaper than scanning 80 candidates after.
2. **Read the shortlist text-first.** `pexels_search_photos` returns up to 15 candidates by default with `alt` text, dimensions and photographer credit. The agent reads the alt strings, drops anything off-topic, and returns the best `image_url` plus the mandatory `photographer` / `photographer_url`.

When the agent commits to a pick, it returns the `image_url` (full resolution) plus the `photographer` and `photographer_url` to honor the [Pexels attribution requirement](https://www.pexels.com/license/).

## Deployment

The server is meant to run as **one hosted HTTPS endpoint** with OAuth 2.1 + RFC 9728 enabled. That is the supported topology — it works for every MCP HTTP client out there (claude.ai web custom connectors, Claude Desktop remote connectors, Claude Code, the MCP Inspector, future clients). Stdio is also functional for power users running a local-only setup; see [Local development](#local-development).

### Auth model in one paragraph — BYOK during the OAuth flow

The Python process plays both roles defined by the MCP authorization spec: it is the **Resource Server** that holds the five Pexels tools at `/mcp`, and the **Authorization Server** that issues short-lived Bearer tokens. The MCP Python SDK mounts every well-known endpoint automatically: `/.well-known/oauth-protected-resource` (RFC 9728), `/.well-known/oauth-authorization-server` (RFC 8414), `/authorize`, `/token`, `/register` (RFC 7591 DCR), all with PKCE.

The flow is **bring-your-own-key** (BYOK): once the MCP client (claude.ai web, Claude Desktop, Claude Code, MCP Inspector) walks the standard OAuth handshake, the server redirects the user's browser to `/setup`, a short HTML form that asks for a Pexels API key. The user pastes their free key (from <https://www.pexels.com/api/>), the server validates it against `api.pexels.com`, then mints the OAuth authorization code with the key bound to it. The bound key follows the code → token transition, so every subsequent tool call resolves the caller's Pexels key from their own access token — no shared secret on the server, no quota theft between users.

For power-user clients that prefer the per-request pattern (Cursor stdio bridges, scripts that already manage Pexels keys themselves), the server still accepts an `X-Pexels-Api-Key` HTTP header as a fallback resolution path.

### Per-call headers (what each MCP client sends)

| Header | When | Purpose |
|---|---|---|
| `Authorization: Bearer <access-token>` | always after the OAuth handshake finishes | Validates the token against the in-memory store. The token is issued by `/token` and refreshed transparently by the client on expiry. The bound Pexels key is resolved from this token. |
| `X-Pexels-Api-Key: <user_key>` | **fallback** for clients that skip the BYOK setup | Optional per-request header. Overridden by the BYOK-bound key when both are present. Get a key at <https://www.pexels.com/api/>. |
| `MCP-Protocol-Version: 2025-11-25` | required by the spec after `initialize` | Tells the server which protocol revision the client speaks. The SDK still accepts 2025-06-18 and 2025-03-26 for downgrade. |

### Server environment variables

| Variable | Required | Description |
|---|---|---|
| `TRANSPORT` | yes | Set to `streamable-http`. |
| `MCP_SERVER_URL` | yes | Public HTTPS URL of this service, no trailing slash (e.g. `https://pexels-mcp.example.com`). Used as both the OAuth `issuer_url` and the RFC 9728 `resource_server_url`. **Must match the host the client sees.** |
| `MCP_ALLOWED_HOSTS` | no | Comma-separated allowlist for the `Host` header (DNS rebinding protection per MCP spec 2025-11-25). Supports the `host:*` wildcard. Unset = accept any Host. |
| `MCP_RATE_LIMIT_PER_MINUTE` | no | Soft DoS guard, default `60`. Max requests/minute/IP. `/healthz`, `/readyz` and the OAuth metadata endpoints are exempt. Set higher if many users share one instance; lower to tighten. |
| `HOST` | no | Default `127.0.0.1`; the Docker image flips it to `0.0.0.0`. |
| `PORT` | no | Default `8000`. Platforms like Koyeb / Fly inject this automatically. |
| `LOG_LEVEL` | no | Default `INFO`. |
| `LOG_FORMAT` | no | `text` or `json` (default `json` in HTTP mode for log-drain ingestion). |
| `MCP_TRUSTED_PROXY_HOPS` | no | Number of trusted proxies in front of the app, default `1` (Koyeb's LB). Used to read the real client IP from `X-Forwarded-For` from the *right* (server-controlled) instead of the *left* (caller-spoofable). Set to `2` if you front Koyeb with Cloudflare; `0` disables `X-Forwarded-For` parsing entirely. |
| `PEXELS_API_KEY` | **stdio only** | Used by local stdio clients (Claude Desktop, Cursor) to inject their key once at process start. **Ignored in `streamable-http` mode** — callers always provide a key via BYOK setup or the `X-Pexels-Api-Key` header. |

### Rate limiting

Each public endpoint (`/mcp`, the OAuth routes, the landing page) is capped at **60 requests/minute per source IP** by default. The cap is a soft DoS guard for a single-instance `eco-nano` Koyeb deployment — beyond the limit the server returns `429 Too Many Requests` with a `Retry-After` header per RFC 9110 §15.5.20. The source IP is read from `X-Forwarded-For` (Koyeb's load balancer sets it) with a fallback to the socket peer.

`/healthz`, `/readyz`, `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server` are **exempt** — platform probes and discovery clients must always reach the server.

Tune via `MCP_RATE_LIMIT_PER_MINUTE`. Pexels' own rate limit (200 req/hour on the caller's key) is a second, complementary line of defense — even a bot that gets through the per-IP cap can't drain anyone's Pexels quota except its own.

### Health and readiness probes

Both `GET /healthz` (liveness) and `GET /readyz` (readiness) return `200 ok` and bypass auth, so platform probes don't trigger 401 noise. The `Dockerfile` declares a `HEALTHCHECK` against `/healthz`. Wire `/readyz` to the platform's "ready for traffic" gate; today both paths behave the same but `/readyz` is reserved for future deeper checks.

### Koyeb deployment

The repo ships a multi-stage `Dockerfile` (Python 3.12 slim, runs as the `app` user, ~80 MB image, `HEALTHCHECK` on `/healthz`, graceful shutdown with a 25 s window — well under Koyeb's 30 s SIGTERM grace period).

#### 1. Create the Koyeb service

Dashboard route (fastest):

1. **Create Service** → **GitHub** source → select this repository, branch `main`.
2. **Builder**: Dockerfile (Koyeb auto-detects).
3. **Instance**: `Nano` is enough — this server is I/O-bound.
4. **Region**: pick the one closest to your callers (e.g. `fra` for EU, `was` for US East).
5. **Ports**: port `8000`, protocol `HTTP`, route `/`.
6. **Health checks**: **HTTP** probe on path `/healthz`, port `8000`. Grace period: 5 s, interval: 30 s.
7. **Environment variables**:

   | Key | Value | Notes |
   |---|---|---|
   | `TRANSPORT` | `streamable-http` | Required. |
   | `MCP_SERVER_URL` | `https://{{ KOYEB_PUBLIC_DOMAIN }}` | Koyeb interpolates this at deploy time; the URL must end up matching what clients hit. |
   | `MCP_ALLOWED_HOSTS` | `{{ KOYEB_PUBLIC_DOMAIN }}` | Origin/Host validation per spec. |
   | `LOG_FORMAT` | `json` | One-line-per-record for the Koyeb log drain. |
   | `LOG_LEVEL` | `INFO` | Bump to `DEBUG` only while diagnosing. |

   Do **not** set `PEXELS_API_KEY` on the server in a multi-tenant deployment.

8. Deploy. Wait for the health check to flip green. The public URL is `https://<service>-<org>.koyeb.app`.

CLI route (reproducible, scriptable):

```bash
koyeb service create pexels-mcp \
  --app pexels-mcp \
  --git github.com/VictorNain26/pexels-mcp-server \
  --git-branch main \
  --git-builder docker \
  --ports 8000:http \
  --routes /:8000 \
  --checks 8000:http:/healthz \
  --env TRANSPORT=streamable-http \
  --env "MCP_SERVER_URL=https://{{ KOYEB_PUBLIC_DOMAIN }}" \
  --env "MCP_ALLOWED_HOSTS={{ KOYEB_PUBLIC_DOMAIN }}" \
  --env LOG_FORMAT=json \
  --instance-type nano \
  --regions fra
```

#### 2. Smoke test the public endpoint

```bash
URL=https://<your-service>.koyeb.app

# Liveness probe (no auth)
curl -s "$URL/healthz"   # -> ok

# RFC 9728 Protected Resource Metadata (no auth)
curl -s "$URL/.well-known/oauth-protected-resource" | head -20
# -> JSON with "resource" and "authorization_servers" fields

# Spec-compliant 401 with WWW-Authenticate pointing to the PRM URL
curl -i -X POST "$URL/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json,text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-11-25' \
  -d '{}' | head -10
# -> HTTP/1.1 401 Unauthorized
# -> WWW-Authenticate: Bearer ... resource_metadata="https://.../.well-known/oauth-protected-resource"
```

The `WWW-Authenticate` header on the unauthenticated `/mcp` call is what makes claude.ai pivot into the OAuth flow.

#### 3. Connect any MCP client (paste your Pexels key once)

| Client | Steps |
|---|---|
| **claude.ai web** | Settings → Connectors → Add custom connector → URL `https://<host>/mcp`. Click *Connect*. A browser tab opens on this server's `/setup` page asking for your Pexels API key — paste it, click *Connect*, the tab closes automatically. The key is bound to your fresh access token for 30 days. |
| **Claude Desktop** | Settings → Connectors → Add (remote) → same URL. Same one-time `/setup` page on first connect; the desktop app handles the OAuth handshake natively. |
| **Claude Code** | `claude mcp add pexels --transport http https://<host>/mcp`. The CLI opens the browser at `/setup` on first call so you can paste your key. |
| **MCP Inspector** | `npx @modelcontextprotocol/inspector` → paste the URL → it discovers OAuth automatically and opens `/setup` in the browser. |

Tokens are valid for 30 days; the client re-runs the OAuth handshake transparently when they expire and you'll be asked for the key again at that point. The bound key also disappears on every server restart (Koyeb rolling deploys, dependency updates) since the store is in-memory only — expect one re-`/setup` per deploy. Power-user setups that prefer to inject the key per call can skip `/setup` and send `X-Pexels-Api-Key` as a header on every request instead — the server falls back to that header when no key is bound to the access token.

## Local development

```bash
git clone https://github.com/VictorNain26/pexels-mcp-server
cd pexels-mcp-server
uv sync --all-extras
```

### Run the HTTP server locally (recommended for parity with prod)

```bash
TRANSPORT=streamable-http \
HOST=127.0.0.1 PORT=8000 \
MCP_SERVER_URL=http://127.0.0.1:8000 \
  uv run pexels-mcp-server
```

Then point any client at `http://127.0.0.1:8000/mcp`. The MCP Inspector is the fastest way to exercise the five tools without going through claude.ai — the OAuth handshake completes silently and you can immediately call tools (after setting `X-Pexels-Api-Key` in the Inspector headers tab).

### Stdio (Cursor and other clients that don't speak MCP HTTP)

```bash
PEXELS_API_KEY=your-key uv run pexels-mcp-server
```

For Cursor, configure a stdio server pointing at this command. Stdio bypasses OAuth entirely — the Pexels key is read directly from the environment. Use it only when the client cannot speak the HTTP transport.

## How a response looks

A `pexels_search_photos` call with `query="paris"`, `per_page=1` returns the following `structuredContent` (and an equivalent JSON-serialized `TextContent` block for backwards compat):

```json
{
  "page": 1,
  "per_page": 1,
  "count": 1,
  "has_more": true,
  "next_page": 2,
  "total_results": 8000,
  "photos": [
    {
      "id": 28448939,
      "alt": "Vibrant street view of central Paris filled with people and traffic on a summer day.",
      "page_url": "https://www.pexels.com/photo/bustling-summer-day-in-central-paris-28448939/",
      "photographer": "Sergey Guk",
      "photographer_url": "https://www.pexels.com/@sergeyguk",
      "width": 4000,
      "height": 6000,
      "image_url": "https://images.pexels.com/photos/28448939/.../original.jpeg"
    }
  ]
}
```

## Three usage examples

### 1. Hero image for a slide deck (with brand color and orientation)

The agent picks the right shot in one tool call by filtering aggressively up front.

```python
pexels_search_photos(
  query="modern open-plan office workspace",
  orientation="landscape",
  size="large",
  color="blue",
  aspect_ratio="16:9",
  per_page=6
)
```

The response is a JSON envelope with up to 6 photos. The agent reads each `alt`, drops the off-topic ones, and returns the best `image_url` plus the mandatory `photographer` / `photographer_url`.

### 2. B-roll video bounded by resolution + aspect ratio

When the user asks for a 4K landscape clip for a hero loop, filter on `size`, `orientation` and `aspect_ratio`.

```python
pexels_search_videos(
  query="aerial drone shot of mountain lake at dawn",
  orientation="landscape",
  size="large",
  aspect_ratio="16:9",
  per_page=10
)
```

Each result carries `video_url` (the direct MP4 of the highest-resolution variant), `duration_seconds`, `quality` and the uploader credit fields.

### 3. Browse a Pexels collection by id

When the user already has a Pexels collection URL (the id sits at the end), drill into its contents.

```python
pexels_get_collection_media(collection_id="<id>", per_page=20)
```

The result splits `photos[]` and `videos[]`. Filter to one type with `type="photos"` or `type="videos"`.

## Rate limits and attribution

Pexels' free tier is **200 requests/hour** on the caller's key. The server logs a warning below 100 remaining (the threshold is set in `client.py`); the per-call response no longer carries a `rate_limit` block to save tokens, but if you ever need it for debugging just enable `LOG_LEVEL=DEBUG`.

If you publish assets returned by this server, you must credit the photographer/videographer and link back to Pexels per the [Pexels guidelines](https://www.pexels.com/license/). Every tool result includes the `photographer` / `uploader_name` and matching URL — surface them in the user-facing answer.

## Tool design notes

- **Spec-compliant auth.** The HTTP transport is an OAuth 2.1 Resource Server *and* Authorization Server in one process, wired through the official MCP Python SDK (`AuthSettings` + `OAuthAuthorizationServerProvider` + `ProviderTokenVerifier`). RFC 9728 Protected Resource Metadata, RFC 8414 Authorization Server Metadata, RFC 7591 Dynamic Client Registration and PKCE are all served by the SDK. The only custom routes are the static landing page at `GET /` and the BYOK setup form at `/setup` that captures the user's Pexels API key during the OAuth flow.
- **Stateless HTTP by default.** The Streamable HTTP transport runs with `stateless_http=True, json_response=True` so a hosted deployment scales horizontally without sticky sessions. The MCP 2026 roadmap calls out stateful sessions as a horizontal-scaling pain point — opting out is the future-proof posture.
- **Read-only by construction.** Every tool advertises `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=true` plus a `title`.
- **Structured tool output.** Each tool returns a typed `dict`. The SDK fills both `structuredContent` (consumed by hosts that read it directly) and a JSON `TextContent` (backwards compat) per MCP spec 2025-11-25 (SHOULD use structured content for parseable data).
- **Token-lean payloads.** Photo responses drop `liked`, `photographer_id`, `avg_color` and the six per-orientation `src` URLs (keeping just `image_url`). Video responses keep only the top file by resolution.
- **Strict inputs.** Every tool argument is validated by Pydantic v2 with `extra="forbid"`; invalid values come back as `isError=true` with `Invalid parameters: <field>: <reason>`.
- **Actionable errors.** Missing key → `Pexels API key is missing. Send it as the 'X-Pexels-Api-Key' header ...`. Rate limit hit → `Pexels rate limit exceeded. Resets at <ISO>. Reduce request frequency.` All errors raise from the tool function so FastMCP marks the `CallToolResult` with `isError=true` per SEP-1303.

## Checks and contributions

Run the full check suite before opening a PR:

```bash
uv run ruff check
uv run ruff format --check
uv run mypy src
uv run pytest
```

Inspect the tools interactively against a running server:

```bash
npx @modelcontextprotocol/inspector
# point it at http://127.0.0.1:8000/mcp once you've launched the dev server
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add a tool. See [SECURITY.md](.github/SECURITY.md) for how to report a vulnerability. See [PRIVACY.md](PRIVACY.md) for what the server processes and what it does not store.

## Compatibility

- Python 3.10, 3.11, 3.12 (CI green on all three).
- `mcp` SDK pinned `>=1.25,<2`. Uses `mcp.server.fastmcp` (the official FastMCP shipped with the SDK, not the unrelated PrefectHQ fork).
- Transport: stdio (default) and Streamable HTTP. Legacy SSE is not enabled.
- MCP spec: 2025-11-25 (the SDK still negotiates downgrade to 2025-06-18 / 2025-03-26).

## License

MIT. See [LICENSE](LICENSE).
