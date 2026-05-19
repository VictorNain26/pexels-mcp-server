# pexels-mcp-server

[![CI](https://github.com/VictorNain26/pexels-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/VictorNain26/pexels-mcp-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-1.27%2B-7c3aed.svg)](https://modelcontextprotocol.io/)

A Model Context Protocol (MCP) server that gives AI agents access to free stock photos and videos from [Pexels](https://www.pexels.com/). Plug it into Claude Desktop, Claude Code, Cursor or any MCP-aware agent and the model gains nine read-only tools to search, browse, resolve and **visually preview** Pexels media.

Designed around Anthropic's [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) guidance: structured JSON responses by default, token-lean payloads (no per-resolution clutter), descriptions written for an LLM caller, actionable error messages, optional vision-driven selection.

## What the agent can do

| Tool | What it does |
|---|---|
| `pexels_search_photos` | Find photos by query, with optional orientation / size / color / locale filters. |
| `pexels_curated_photos` | Browse Pexels' daily editor pick when the user has no specific topic. |
| `pexels_get_photo` | Resolve a photo id to its canonical record. |
| `pexels_search_videos` | Find videos by query, with orientation / size / locale filters. |
| `pexels_popular_videos` | Browse trending videos, optionally bounded by resolution or duration. |
| `pexels_get_video` | Resolve a video id to its canonical record. |
| `pexels_list_featured_collections` | Discover themed media bundles. |
| `pexels_get_collection_media` | Read the contents of a specific collection. |
| `pexels_preview_media` | Fetch thumbnails of search results as inline images so a vision-capable agent picks visually. |

Every search/list tool returns a JSON envelope with `total_results`, `has_more`, `next_page` and a `rate_limit` block, so the agent can paginate and self-pace.

## How the agent picks the best image

Pexels already ranks search results by relevance. On top of that, the tools are shaped to let the agent reason its way to the right shot in three steps:

1. **Frame the query and filters.** The agent should translate the user's request into a tight search term plus the filters that matter (`orientation` for hero banners, `color` for brand fit, `size` if the user wants print-quality, `min_duration` for videos that need to last a certain time). Aggressive filtering on the first call is cheaper than scanning 80 candidates afterwards.
2. **Read the shortlist text-first.** `pexels_search_photos` returns up to 15 candidates by default with `alt` text, dimensions and photographer credit. For most picks this is enough: the agent reads the alt strings, drops anything off-topic, and keeps 2-6 candidates.
3. **(Optional) confirm visually with `pexels_preview_media`.** When the user needs the "right" shot and the alt text alone is ambiguous, the agent passes the shortlisted `thumbnail_url` values (or `preview_image_url` for videos) into `pexels_preview_media`. The tool fetches each thumbnail from `images.pexels.com`, returns them as inline `ImageContent`, and the vision-capable model picks the winner visually. The path is whitelisted (no SSRF) and the payload is capped at 6 thumbnails per call.

When the agent commits to a pick, it returns the `image_url` (full resolution) plus the `photographer` and `photographer_url` to honor the [Pexels attribution requirement](https://www.pexels.com/license/).

## Deployment

The server is meant to run as **one hosted HTTPS endpoint** with OAuth 2.1 + RFC 9728 enabled. That is the only supported topology — it works for every MCP HTTP client out there (claude.ai web custom connectors, Claude Desktop remote connectors, Claude Code, the MCP Inspector, future clients). Stdio is still functional and useful for power users who want a local-only setup; see [Local development](#local-development).

### Architecture in one paragraph

The Python process plays both roles defined by the MCP authorization spec: it is the **Resource Server** that holds the nine Pexels tools at `/mcp`, and the **Authorization Server** that issues short-lived Bearer tokens. The MCP Python SDK mounts the well-known metadata endpoints (`/.well-known/oauth-protected-resource` per RFC 9728, `/.well-known/oauth-authorization-server` per RFC 8414) and the OAuth routes (`/authorize`, `/token`, `/register`) automatically. The only custom touch is a minimal `/login` HTML page where the human user types a shared passcode — the OAuth flow then proceeds end-to-end.

### Per-call headers (what each MCP client sends)

| Header | When | Purpose |
|---|---|---|
| `Authorization: Bearer <access-token>` | always after the OAuth flow finishes | Validates the token against the in-memory store. The token is issued by `/token` after a successful passcode login; the client refreshes it on expiry by re-running `/authorize`. |
| `X-Pexels-Api-Key: <user_key>` | required on every tool call | The caller's own Pexels key, used to authenticate the upstream Pexels REST calls. Not stored, not logged. Get one at <https://www.pexels.com/api/>. |
| `MCP-Protocol-Version: 2025-06-18` | required by the spec after `initialize` | Tells the server which protocol revision the client speaks. |

### Server environment variables

| Variable | Required | Description |
|---|---|---|
| `TRANSPORT` | yes | Set to `streamable-http`. |
| `MCP_SERVER_URL` | yes | Public HTTPS URL of this service, no trailing slash (e.g. `https://pexels-mcp.example.com`). Used as both the OAuth `issuer_url` and the RFC 9728 `resource_server_url`. **Must match the host the client sees.** |
| `MCP_AUTH_PASSCODE` | yes | Shared secret the user types on the `/login` page during the OAuth flow. Generate with `openssl rand -hex 16`. Distribute out of band. |
| `MCP_ALLOWED_HOSTS` | no | Comma-separated allowlist for the `Host` header (DNS rebinding protection per MCP spec 2025-06-18). Supports the `host:*` wildcard. Unset = accept any Host. |
| `HOST` | no | Default `127.0.0.1`; the Docker image flips it to `0.0.0.0`. |
| `PORT` | no | Default `8000`. Platforms like Koyeb / Fly inject this automatically. |
| `LOG_LEVEL` | no | Default `INFO`. |
| `LOG_FORMAT` | no | `text` or `json` (default `json` in HTTP mode for log-drain ingestion). |
| `PEXELS_API_KEY` | no | Server-side fallback key for callers who omit `X-Pexels-Api-Key`. Leave unset for multi-tenant deployments — each caller pays its own Pexels quota. |

### Health and readiness probes

Both `GET /healthz` (liveness) and `GET /readyz` (readiness) return `200 ok` and bypass auth, so platform probes don't trigger 401 noise. The `Dockerfile` declares a `HEALTHCHECK` against `/healthz`. Wire `/readyz` to the platform's "ready for traffic" gate; today both paths behave the same but `/readyz` is reserved for future deeper checks.

### Koyeb deployment

The repo ships a multi-stage `Dockerfile` (Python 3.12 slim, runs as the `app` user, ~80 MB image, `HEALTHCHECK` on `/healthz`, graceful shutdown with a 25 s window — well under Koyeb's 30 s SIGTERM grace period).

#### 1. Generate the passcode

```bash
openssl rand -hex 16
```

Save it. You'll set it as `MCP_AUTH_PASSCODE` on Koyeb and type it on the `/login` page the first time you connect any MCP client.

#### 2. Create the Koyeb service

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
   | `MCP_AUTH_PASSCODE` | `<paste the openssl output>` | Mark as **Secret**. |
   | `MCP_ALLOWED_HOSTS` | `{{ KOYEB_PUBLIC_DOMAIN }}` | Origin/Host validation per spec. |
   | `LOG_FORMAT` | `json` | One-line-per-record for the Koyeb log drain. |
   | `LOG_LEVEL` | `INFO` | Bump to `DEBUG` only while diagnosing. |

   Do **not** set `PEXELS_API_KEY` on the server in a multi-tenant deployment.

8. Deploy. Wait for the health check to flip green. The public URL is `https://<service>-<org>.koyeb.app`.

CLI route (reproducible, scriptable):

```bash
koyeb secret create mcp-auth-passcode --value <hex from openssl>

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
  --env "MCP_AUTH_PASSCODE=@mcp-auth-passcode" \
  --instance-type nano \
  --regions fra
```

#### 3. Smoke test the public endpoint

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
  -H 'MCP-Protocol-Version: 2025-06-18' \
  -d '{}' | head -10
# -> HTTP/1.1 401 Unauthorized
# -> WWW-Authenticate: Bearer ... resource_metadata="https://.../.well-known/oauth-protected-resource"
```

The `WWW-Authenticate` header on the unauthenticated `/mcp` call is what makes claude.ai pivot into the OAuth flow.

#### 4. Connect any MCP client

| Client | Steps |
|---|---|
| **claude.ai web** | Settings → Connectors → Add custom connector → URL `https://<host>/mcp`. Click *Connect* → browser pops the `/login` page → enter the passcode → tokens exchanged automatically. Then add `X-Pexels-Api-Key: <your key>` under Advanced custom headers. |
| **Claude Desktop** | Settings → Connectors → Add (remote) → same URL and passcode flow. Custom headers tab takes `X-Pexels-Api-Key`. |
| **Claude Code** | `claude mcp add pexels --transport http https://<host>/mcp --header "X-Pexels-Api-Key: <key>"`. The CLI walks you through the OAuth login. |
| **MCP Inspector** | `npx @modelcontextprotocol/inspector` → paste the URL → it discovers OAuth automatically. |

If a token expires (default 1 h), the client re-runs the flow transparently.

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
MCP_AUTH_PASSCODE=devpass \
  uv run pexels-mcp-server
```

Then point any client at `http://127.0.0.1:8000/mcp`. The `/login` page accepts `devpass`. The MCP Inspector is the fastest way to exercise the 9 tools without going through claude.ai.

### Stdio (Cursor and other clients that don't speak MCP HTTP)

```bash
PEXELS_API_KEY=your-key uv run pexels-mcp-server
```

For Cursor, configure a stdio server pointing at this command. Stdio bypasses OAuth entirely — the Pexels key is read directly from the environment. Use it only when the client cannot speak the HTTP transport.

## How a response looks

A `pexels_search_photos` call with `query="paris"`, `per_page=1` returns:

```json
{
  "total_results": 8000,
  "page": 1,
  "per_page": 1,
  "count": 1,
  "has_more": true,
  "next_page": 2,
  "rate_limit": { "limit": 25000, "remaining": 24996, "reset": "2026-06-17T21:45:48+00:00" },
  "photos": [
    {
      "id": 28448939,
      "alt": "Vibrant street view of central Paris filled with people and traffic on a summer day.",
      "page_url": "https://www.pexels.com/photo/bustling-summer-day-in-central-paris-28448939/",
      "photographer": "Sergey Guk",
      "photographer_url": "https://www.pexels.com/@sergeyguk",
      "width": 4000,
      "height": 6000,
      "image_url": "https://images.pexels.com/photos/28448939/.../original.jpeg",
      "thumbnail_url": "https://images.pexels.com/photos/28448939/.../medium.jpeg"
    }
  ]
}
```

Switch to `response_format="markdown"` if you want a one-line human summary instead.

## Three usage examples

### 1. Hero image for a slide deck (with brand color and orientation)

The agent picks the right shot in one tool call by filtering aggressively up front.

```
pexels_search_photos(
  query="modern open-plan office workspace",
  orientation="landscape",
  size="large",
  color="blue",
  per_page=6
)
```

The response is a JSON envelope with up to 6 photos. The agent reads each `alt` field, drops the off-topic ones, and returns the best `image_url` plus the mandatory `photographer` / `photographer_url` for attribution.

### 2. B-roll video bounded by duration and resolution

When the user asks for a 10-15 second loop in 4K, filtering on `min_duration`, `max_duration` and `size` avoids scanning hundreds of candidates.

```
pexels_search_videos(
  query="aerial drone shot of mountain lake at dawn",
  orientation="landscape",
  size="large",
  per_page=10
)
```

Then, since the search tool already trims to the top 3 files by resolution, the agent reads `files[0].url` for the highest-quality MP4 stream and `duration_seconds` to confirm length before committing.

### 3. Visual pick after an ambiguous text shortlist

When the user wants *the right* shot and `alt` text alone can't decide between candidates, the agent passes the shortlisted `thumbnail_url` values into the visual-pick tool. The thumbnails come back inline as `ImageContent` blocks and a vision-capable model picks the winner.

```
# Step 1: search and read alt text
photos = pexels_search_photos(query="minimalist desk setup", per_page=4)

# Step 2: extract the 4 thumbnail_url values and call:
pexels_preview_media(
  thumbnail_urls=[
    "https://images.pexels.com/photos/.../medium.jpeg",
    "https://images.pexels.com/photos/.../medium.jpeg",
    "https://images.pexels.com/photos/.../medium.jpeg",
    "https://images.pexels.com/photos/.../medium.jpeg",
  ]
)
```

URLs are checked at validation time: only `https://images.pexels.com` is accepted, every other host is rejected before any network call (no SSRF surface). Each thumbnail is capped at 256 KB and the batch is capped at 6 URLs.

## Rate limits and attribution

Pexels' free tier is **200 requests/hour** and **20 000 requests/month**. Every tool response surfaces `rate_limit.remaining` so the agent can decide whether to keep calling; a warning is logged below 100 remaining.

If you publish assets returned by this server, you must credit the photographer/videographer and link back to Pexels per the [Pexels guidelines](https://www.pexels.com/license/). The Markdown output appends the required `Photos provided by Pexels` footer automatically.

## Tool design notes

- **Spec-compliant auth.** The HTTP transport is an OAuth 2.1 Resource Server *and* Authorization Server in one process, wired through the official MCP Python SDK (`AuthSettings` + `OAuthAuthorizationServerProvider` + `ProviderTokenVerifier`). RFC 9728 Protected Resource Metadata, RFC 8414 Authorization Server Metadata, RFC 7591 Dynamic Client Registration and PKCE are all served by the SDK; the only custom code is the `/login` passcode form.
- **Stateless HTTP by default.** The Streamable HTTP transport runs with `stateless_http=True, json_response=True` so a hosted deployment scales horizontally without sticky sessions. The MCP 2025-06-18 spec keeps `Mcp-Session-Id` as OPTIONAL; opting out is the SDK-recommended posture.
- **Read-only by construction.** Every tool advertises `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=true`.
- **Token-lean payloads.** Photo responses drop `liked`, `photographer_id`, `avg_color` and the six per-orientation `src` URLs (keeping just `image_url` and `thumbnail_url`). Video responses keep only the top 3 files by resolution and report `total_files_available` so the agent knows there's more.
- **Strict inputs.** Every tool argument is validated by Pydantic v2 with `extra="forbid"`; invalid values come back as `Invalid parameters: <field>: <reason>` rather than a raw exception.
- **Actionable errors.** Missing key → `Pexels API key is invalid or missing. Set PEXELS_API_KEY ...`. Rate limit hit → `Pexels rate limit exceeded. Resets at <ISO>. Reduce request frequency.`
- **No SSRF.** `pexels_preview_media` rejects any URL whose host is not `images.pexels.com` at validation time, before the network fetch.

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

## License

MIT. See [LICENSE](LICENSE).
