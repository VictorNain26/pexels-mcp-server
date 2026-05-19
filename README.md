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

## Quick install (local stdio)

You'll need a Pexels API key (free, request one at <https://www.pexels.com/api/>) and [uv](https://docs.astral.sh/uv/) for the simplest local setup.

### Claude Desktop

Edit `claude_desktop_config.json` (Mac: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "pexels": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/VictorNain26/pexels-mcp-server", "pexels-mcp-server"],
      "env": { "PEXELS_API_KEY": "your-key-here" }
    }
  }
}
```

Restart Claude Desktop.

### Claude Code

```bash
claude mcp add pexels \
  -e PEXELS_API_KEY=your-key-here \
  -- uvx --from git+https://github.com/VictorNain26/pexels-mcp-server pexels-mcp-server
```

### Cursor / other MCP clients

Same pattern: configure a stdio server with `command=uvx`, the `--from git+...` args above, and `PEXELS_API_KEY` in the environment.

## Hosted deployment (Streamable HTTP)

For the [claude.ai web "custom connectors" UI](https://claude.ai/) you need a public HTTPS endpoint. The server speaks Streamable HTTP and ships with a `Dockerfile`, Bearer auth middleware and a per-request Pexels-key extractor.

The hosted deployment **does not hold a default Pexels API key**. Every caller is required to send their own via the `X-Pexels-Api-Key` request header. The server forwards the header to Pexels and never persists it. This keeps the deployment multi-tenant safe: each user pays their own Pexels quota.

### Per-request headers

| Header | Required | Purpose |
|---|---|---|
| `Authorization: Bearer <MCP_AUTH_TOKEN>` | when `MCP_AUTH_TOKEN` is set on the server | Resource gate for the host (prevents random internet traffic). |
| `X-Pexels-Api-Key: <user_key>` | yes | The caller's own Pexels key. Used to authenticate the upstream Pexels REST calls. Get one at <https://www.pexels.com/api/>. |

### Server environment variables

| Variable | Required | Description |
|---|---|---|
| `MCP_AUTH_TOKEN` | **required** in HTTP mode | Shared Bearer token. The `/mcp` endpoint refuses to boot without it; the process exits with code 2. Generate with `openssl rand -hex 32`. Set `MCP_ALLOW_UNAUTHED=1` to override for local development. |
| `MCP_ALLOW_UNAUTHED` | no | Set to `1` to allow the server to boot without `MCP_AUTH_TOKEN` (development only — never in production). |
| `MCP_ALLOWED_HOSTS` | no | Comma-separated allowlist for the `Host` header (DNS rebinding protection). Supports the `host:*` wildcard. Unset means accept any Host (Bearer auth is the gate). |
| `TRANSPORT` | yes (HTTP mode) | Set to `streamable-http`. |
| `HOST` | no | Default `127.0.0.1`; the Docker image flips it to `0.0.0.0`. |
| `PORT` | no | Default `8000`. Platforms like Koyeb / Fly inject this automatically. |
| `LOG_LEVEL` | no | Default `INFO`. |
| `LOG_FORMAT` | no | `text` (default for stdio) or `json` (default for streamable-http). Pick `json` for log-drain ingestion on Koyeb / Fly / Cloud Run. |
| `PEXELS_API_KEY` | no | Server-side fallback key for callers who omit the `X-Pexels-Api-Key` header. Leave unset for multi-tenant deployments. |

### Health and readiness probes

Both `GET /healthz` (liveness) and `GET /readyz` (readiness) return `200 ok` and bypass auth, so platform probes don't trigger 401 noise. The `Dockerfile` declares a `HEALTHCHECK` against `/healthz`. Wire `/readyz` to the platform's "ready for traffic" gate; today both paths behave the same but `/readyz` is reserved for future deeper checks (Pexels reachability, key validity).

### Koyeb deployment guide

The repo ships a multi-stage `Dockerfile` (Python 3.12 slim, runs as the `app` user, ~80 MB image, `HEALTHCHECK` on `/healthz`, graceful shutdown with a 25 s window — well under Koyeb's 30 s SIGTERM grace period).

#### 1. Generate the Bearer token

```bash
openssl rand -hex 32
```

Keep it. You'll need it on the Koyeb side (as `MCP_AUTH_TOKEN`) and on the claude.ai connector side (as `Authorization: Bearer <token>`).

#### 2. Create the Koyeb service

Dashboard route (fastest):

1. **Create Service** → **GitHub** source → select this repository, branch `main`.
2. **Builder**: Dockerfile (Koyeb auto-detects).
3. **Instance**: `Nano` is enough (this server is I/O-bound, a few MB of RAM per request).
4. **Region**: pick the one closest to your callers (e.g. `fra` for EU, `was` for US East).
5. **Ports**:
   - Port `8000`, protocol `HTTP`, route `/`.
6. **Health checks**:
   - **HTTP** probe on path `/healthz`, port `8000`. Use HTTP, not the default TCP — TCP just confirms the socket is open, HTTP confirms the app booted. Grace period: 5 s, interval: 30 s.
7. **Environment variables** (all plain text except the secrets):

   | Key | Value | Notes |
   |---|---|---|
   | `TRANSPORT` | `streamable-http` | Required to switch off stdio mode. |
   | `MCP_AUTH_TOKEN` | `<paste the openssl output>` | Mark as **Secret**. |
   | `MCP_ALLOWED_HOSTS` | `{{ KOYEB_PUBLIC_DOMAIN }}` | Re-enables Origin/Host validation per MCP spec 2025-06-18. Koyeb substitutes its public domain at runtime. |
   | `LOG_FORMAT` | `json` | One-line-per-record for the Koyeb log drain. |
   | `LOG_LEVEL` | `INFO` | Bump to `DEBUG` only while diagnosing. |

   Do **not** set `PEXELS_API_KEY` on the server in a multi-tenant deployment — each caller sends their own `X-Pexels-Api-Key` header and pays their own quota.

8. Deploy. Wait for the health check to flip green. The public URL is `https://<service-name>-<org-slug>.koyeb.app`.

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
  --env "MCP_ALLOWED_HOSTS={{ KOYEB_PUBLIC_DOMAIN }}" \
  --env LOG_FORMAT=json \
  --env "MCP_AUTH_TOKEN=@mcp-auth-token" \
  --instance-type nano \
  --regions fra
```

Create the `mcp-auth-token` secret first with `koyeb secret create mcp-auth-token --value <hex>`. The `@secret-name` syntax injects it as an env var without exposing it in the service definition.

#### 3. Smoke test the public endpoint

```bash
# Public URL Koyeb gave you
URL=https://<your-service>.koyeb.app
TOKEN=<the bearer token>
PEXELS=<your Pexels key>

curl -s "$URL/healthz"   # → ok

curl -s -H "Authorization: Bearer $TOKEN" \
     -H "X-Pexels-Api-Key: $PEXELS" \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json,text/event-stream' \
     -H 'MCP-Protocol-Version: 2025-06-18' \
     -X POST "$URL/mcp" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
```

The `initialize` call should return a JSON envelope with `result.serverInfo.name = "pexels-mcp-server"`. If you get `401`, the Bearer token is wrong. If you get the Pexels auth error, `X-Pexels-Api-Key` is missing or invalid.

#### 4. Wire it into claude.ai

1. claude.ai → **Settings → Connectors → Add custom connector**.
2. URL: `https://<your-service>.koyeb.app/mcp`.
3. Authentication → **Custom headers**:
   - `Authorization: Bearer <MCP_AUTH_TOKEN>`
   - `X-Pexels-Api-Key: <your Pexels key>`
4. Save, enable in a conversation, prompt: « find me three landscape photos of Paris on Pexels ».

#### Local dry-run (before deploying)

```bash
MCP_AUTH_TOKEN=test123 TRANSPORT=streamable-http HOST=0.0.0.0 \
  uv run pexels-mcp-server
# in another terminal
curl -s http://localhost:8000/healthz
curl -s -H 'Authorization: Bearer test123' \
     -H 'X-Pexels-Api-Key: your_pexels_key' \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json,text/event-stream' \
     -H 'MCP-Protocol-Version: 2025-06-18' \
     -X POST http://localhost:8000/mcp \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
```

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

- **Stateless HTTP by default.** The Streamable HTTP transport runs with `stateless_http=True, json_response=True` so a hosted deployment scales horizontally on Koyeb / Fly without sticky sessions. Aligned with the MCP draft spec direction (session IDs removed).
- **Read-only by construction.** Every tool advertises `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=true`.
- **Token-lean payloads.** Photo responses drop `liked`, `photographer_id`, `avg_color` and the six per-orientation `src` URLs (keeping just `image_url` and `thumbnail_url`). Video responses keep only the top 3 files by resolution and report `total_files_available` so the agent knows there's more.
- **Strict inputs.** Every tool argument is validated by Pydantic v2 with `extra="forbid"`; invalid values come back as `Invalid parameters: <field>: <reason>` rather than a raw exception.
- **Actionable errors.** Missing key → `Pexels API key is invalid or missing. Set PEXELS_API_KEY ...`. Rate limit hit → `Pexels rate limit exceeded. Resets at <ISO>. Reduce request frequency.`
- **No SSRF.** `pexels_preview_media` rejects any URL whose host is not `images.pexels.com` at validation time, before the network fetch.

## Development

```bash
git clone https://github.com/VictorNain26/pexels-mcp-server
cd pexels-mcp-server
uv sync --all-extras

uv run ruff check
uv run ruff format --check
uv run mypy src
uv run pytest
```

Run the server against the local checkout:

```bash
PEXELS_API_KEY=your-key uv run pexels-mcp-server
```

Inspect tool schemas interactively:

```bash
npx @modelcontextprotocol/inspector uv run pexels-mcp-server
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add a tool. See [SECURITY.md](.github/SECURITY.md) for how to report a vulnerability. See [PRIVACY.md](PRIVACY.md) for what the server processes and what it does not store.

## Compatibility

- Python 3.10, 3.11, 3.12 (CI green on all three).
- `mcp` SDK pinned `>=1.25,<2`. Uses `mcp.server.fastmcp` (the official FastMCP shipped with the SDK, not the unrelated PrefectHQ fork).
- Transport: stdio (default) and Streamable HTTP. Legacy SSE is not enabled.

## License

MIT. See [LICENSE](LICENSE).
