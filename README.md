# pexels-mcp-server

A Model Context Protocol (MCP) server that gives AI agents access to free stock photos and videos from [Pexels](https://www.pexels.com/). Plug it into Claude Desktop, Claude Code, Cursor, or any MCP-aware agent and the model gains eight read-only tools to search, browse and resolve Pexels media.

Designed around Anthropic's [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) guidance: structured JSON responses by default, token-lean payloads (no per-resolution clutter), descriptions written for an LLM caller, actionable error messages.

## What the agent can do

| Tool | What it does |
|---|---|
| `pexels_search_photos` | Find photos by query, with optional orientation / size / color / locale filters. |
| `pexels_curated_photos` | Browse Pexels' daily editor pick when the user has no specific topic. |
| `pexels_get_photo` | Resolve a photo id to its canonical record (alt text, dimensions, credit, full-res URL). |
| `pexels_search_videos` | Find videos by query, with orientation / size / locale filters. |
| `pexels_popular_videos` | Browse trending videos, optionally bounded by resolution or duration. |
| `pexels_get_video` | Resolve a video id to its canonical record (duration, top 3 file URLs, uploader). |
| `pexels_list_featured_collections` | Discover themed media bundles. |
| `pexels_get_collection_media` | Read the contents of a specific collection. |

Every tool returns a JSON envelope with `total_results`, `has_more`, `next_page` and a `rate_limit` block, so the agent can paginate and self-pace.

## Quick install

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

Restart Claude Desktop and the eight tools appear in the tools picker.

### Claude Code

```bash
claude mcp add pexels \
  -e PEXELS_API_KEY=your-key-here \
  -- uvx --from git+https://github.com/VictorNain26/pexels-mcp-server pexels-mcp-server
```

Run `claude mcp list` to confirm it shows `Connected`.

### Cursor / other MCP clients

Same pattern: configure a stdio server with `command=uvx`, the `--from git+...` args above, and `PEXELS_API_KEY` in the environment.

### Remote (Streamable HTTP)

The server also speaks Streamable HTTP for hosted deployments and the claude.ai web "connectors" UI:

```bash
PEXELS_API_KEY=your-key TRANSPORT=streamable-http PORT=8000 \
  uvx --from git+https://github.com/VictorNain26/pexels-mcp-server pexels-mcp-server
```

Deploy this behind HTTPS (Koyeb, Fly.io, Railway, your own reverse proxy) and point the agent at `https://<host>/mcp`. Protect the route with a Bearer header or your platform's auth; the Pexels key stays server-side.

## How the agent sees a response

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

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PEXELS_API_KEY` | required | Your Pexels API key. |
| `TRANSPORT` | `stdio` | `stdio` or `streamable-http`. |
| `HOST` | `127.0.0.1` | Bind host for `streamable-http`. |
| `PORT` | `8000` | Bind port for `streamable-http`. |
| `LOG_LEVEL` | `INFO` | Standard Python log level. Logs go to stderr (stdout is reserved for JSON-RPC). |

## Rate limits and attribution

Pexels' free tier is **200 requests/hour** and **20 000 requests/month**. Every tool response surfaces `rate_limit.remaining` so the agent can decide whether to keep calling. A warning is logged to stderr below 100 remaining.

If you publish images or videos returned by this server, you must credit the photographer or videographer and link back to Pexels per the [Pexels guidelines](https://www.pexels.com/license/). The Markdown output includes the required `Photos provided by Pexels` footer automatically.

## Tool design notes

- **Read-only by construction.** Every tool advertises `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=true`.
- **Token-lean payloads.** Photo responses drop `liked`, `photographer_id`, `avg_color` and the six per-orientation `src` URLs (keeping just `image_url` and `thumbnail_url`). Video responses keep only the top 3 files by resolution and report `total_files_available` so the agent knows there's more.
- **Strict inputs.** Every tool argument is validated by Pydantic v2 with `extra="forbid"`; invalid values come back as `Invalid parameters: <field>: <reason>` rather than a raw exception.
- **Actionable errors.** Missing key → `Pexels API key is invalid or missing. Set PEXELS_API_KEY ...`. Rate limit hit → `Pexels rate limit exceeded. Resets at <ISO>. Reduce request frequency.`

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

## Compatibility

- Python 3.10, 3.11, 3.12 (CI green on all three).
- `mcp` SDK pinned `>=1.25,<2`. Uses `mcp.server.fastmcp` (the official FastMCP shipped with the SDK, not the unrelated PrefectHQ fork).
- Transport: stdio (default) and Streamable HTTP. Legacy SSE is not enabled.

## License

MIT. See [LICENSE](LICENSE).
