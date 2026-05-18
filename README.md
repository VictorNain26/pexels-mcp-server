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

For the [claude.ai web "custom connectors" UI](https://claude.ai/) you need a public HTTPS endpoint. The server speaks Streamable HTTP and ships with a `Dockerfile` + Bearer auth middleware.

### Configuration

| Variable | Required | Description |
|---|---|---|
| `PEXELS_API_KEY` | yes | Your Pexels API key. Stays server-side. |
| `MCP_AUTH_TOKEN` | recommended | Shared Bearer token. When set, the `/mcp` endpoint requires `Authorization: Bearer <token>`. When unset, the endpoint is open to anyone who can reach the host. Generate with `openssl rand -hex 32`. |
| `MCP_ALLOWED_HOSTS` | no | Comma-separated allowlist for the `Host` header (DNS rebinding protection). Supports the `host:*` wildcard. Unset means accept any Host (Bearer auth is the gate). |
| `TRANSPORT` | yes (HTTP mode) | Set to `streamable-http`. |
| `HOST` | no | Default `127.0.0.1`; the Docker image flips it to `0.0.0.0`. |
| `PORT` | no | Default `8000`. Platforms like Koyeb / Fly inject this automatically. |
| `LOG_LEVEL` | no | Default `INFO`. |

### Health probe

A `GET /healthz` route returns `200 ok` and bypasses auth, so platform liveness probes don't trigger 401 noise. The `Dockerfile` declares a `HEALTHCHECK` against it.

### Koyeb (or any container platform)

The repo ships a multi-stage `Dockerfile` (Python 3.12 slim, runs as the `app` user, ~80 MB). On Koyeb:

1. Create an app pointing at this GitHub repo (Dockerfile builder).
2. Add the env vars above (`PEXELS_API_KEY`, `MCP_AUTH_TOKEN`, `TRANSPORT=streamable-http`).
3. Expose port `8000` over HTTPS. Koyeb terminates TLS for you.
4. Use the public URL `https://<your-app>.koyeb.app/mcp` as the connector endpoint in claude.ai, with the Bearer token in the `Authorization` header.

Local dry-run:

```bash
PEXELS_API_KEY=... MCP_AUTH_TOKEN=test123 TRANSPORT=streamable-http HOST=0.0.0.0 \
  uv run pexels-mcp-server
# in another terminal
curl -s http://localhost:8000/healthz
curl -s -H 'Authorization: Bearer test123' http://localhost:8000/mcp -X POST \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{}}}'
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

## Rate limits and attribution

Pexels' free tier is **200 requests/hour** and **20 000 requests/month**. Every tool response surfaces `rate_limit.remaining` so the agent can decide whether to keep calling; a warning is logged below 100 remaining.

If you publish assets returned by this server, you must credit the photographer/videographer and link back to Pexels per the [Pexels guidelines](https://www.pexels.com/license/). The Markdown output appends the required `Photos provided by Pexels` footer automatically.

## Tool design notes

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

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add a tool. See [SECURITY.md](.github/SECURITY.md) for how to report a vulnerability.

## Compatibility

- Python 3.10, 3.11, 3.12 (CI green on all three).
- `mcp` SDK pinned `>=1.25,<2`. Uses `mcp.server.fastmcp` (the official FastMCP shipped with the SDK, not the unrelated PrefectHQ fork).
- Transport: stdio (default) and Streamable HTTP. Legacy SSE is not enabled.

## License

MIT. See [LICENSE](LICENSE).
