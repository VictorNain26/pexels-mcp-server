# pexels-mcp-server

Model Context Protocol (MCP) server for the [Pexels API](https://www.pexels.com/api/). Exposes free stock photos and videos to MCP-aware clients (Claude Code, Claude Desktop, any MCP runtime).

## Features

Eight read-only tools, all namespaced `pexels_`:

- `pexels_search_photos` - search free stock photos by query, orientation, size, color, locale.
- `pexels_curated_photos` - editor-curated photo feed.
- `pexels_get_photo` - fetch a single photo by id.
- `pexels_search_videos` - search free stock videos.
- `pexels_popular_videos` - currently popular videos with size/duration filters.
- `pexels_get_video` - fetch a single video by id.
- `pexels_list_featured_collections` - browse themed collections.
- `pexels_get_collection_media` - list photos and videos inside a collection.

Each tool accepts `response_format="markdown"` (default, human-readable) or `response_format="json"` (machine-readable envelope with `total_results`, `has_more`, `next_page`, `rate_limit`).

## Requirements

- Python 3.10+
- A free Pexels API key. Request one at <https://www.pexels.com/api/>.

## Installation

The recommended way is via [uv](https://docs.astral.sh/uv/):

```bash
uvx pexels-mcp
```

Or install with pip:

```bash
pip install pexels-mcp
pexels-mcp-server
```

## Configuration

### Claude Desktop

Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pexels": {
      "command": "uvx",
      "args": ["pexels-mcp"],
      "env": {
        "PEXELS_API_KEY": "your-key-here"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add pexels -e PEXELS_API_KEY=your-key-here -- uvx pexels-mcp
```

### Streamable HTTP (remote)

```bash
PEXELS_API_KEY=your-key-here TRANSPORT=streamable-http PORT=8000 uvx pexels-mcp
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `PEXELS_API_KEY` | required | Your Pexels API key. |
| `TRANSPORT` | `stdio` | `stdio` or `streamable-http`. |
| `HOST` | `127.0.0.1` | Bind host for `streamable-http`. |
| `PORT` | `8000` | Bind port for `streamable-http`. |
| `LOG_LEVEL` | `INFO` | Standard Python log level. Logs go to stderr only. |

## Tool reference

All tools are read-only (`readOnlyHint=true`, `idempotentHint=true`, `openWorldHint=true`) and surface the upstream rate limit in the response envelope.

| Tool | Required args | Optional args |
|---|---|---|
| `pexels_search_photos` | `query` | `orientation`, `size`, `color`, `locale`, `page`, `per_page`, `response_format` |
| `pexels_curated_photos` | - | `page`, `per_page`, `response_format` |
| `pexels_get_photo` | `photo_id` | `response_format` |
| `pexels_search_videos` | `query` | `orientation`, `size`, `locale`, `page`, `per_page`, `response_format` |
| `pexels_popular_videos` | - | `min_width`, `min_height`, `min_duration`, `max_duration`, `page`, `per_page`, `response_format` |
| `pexels_get_video` | `video_id` | `response_format` |
| `pexels_list_featured_collections` | - | `page`, `per_page`, `response_format` |
| `pexels_get_collection_media` | `collection_id` | `type`, `sort`, `page`, `per_page`, `response_format` |

`per_page` accepts 1-80 (default 15).

## Rate limits

Pexels caps free keys at **200 requests/hour** and **20 000 requests/month**. Every tool response includes a `rate_limit` block with `limit`, `remaining` and `reset` (ISO 8601 UTC). When fewer than 100 requests remain the server logs a warning to stderr.

## Attribution

Photos and videos returned by these tools come from Pexels. Per the [Pexels guidelines](https://www.pexels.com/license/) you should credit the photographer and link back to Pexels whenever the assets are published. The Markdown formatters include `Photos provided by Pexels (https://www.pexels.com)` as a footer automatically.

## Differences from `pexels-mcp-server` (garylab)

A [previous Python implementation](https://github.com/garylab/pexels-mcp-server) exists on PyPI under the `pexels-mcp-server` name but has not been updated since August 2025. This project ships under the PyPI name `pexels-mcp` and adds:

- MCP SDK pinned to `>=1.25,<2` (current `mcp.server.fastmcp` API).
- Strict Pydantic v2 inputs with `extra="forbid"`.
- `response_format` switch (markdown or JSON) on every tool.
- Streamable HTTP transport ready out of the box.
- Async `httpx` client with retry on 5xx and rate-limit parsing.
- Full test suite (pytest + pytest-httpx), `mypy --strict`, `ruff`.

## Development

```bash
uv sync --all-extras
uv run ruff check
uv run ruff format --check
uv run mypy src
uv run pytest
```

Run locally without publishing:

```bash
uvx --from . pexels-mcp
```

## License

MIT. See [LICENSE](LICENSE).
