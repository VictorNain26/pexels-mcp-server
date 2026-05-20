# pexels-mcp-server

[![CI](https://github.com/VictorNain26/pexels-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/VictorNain26/pexels-mcp-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-2025--11--25-7c3aed.svg)](https://modelcontextprotocol.io/specification/2025-11-25)

A Model Context Protocol (MCP) server that gives AI agents access to free
stock photos and videos from [Pexels](https://www.pexels.com/). Plug it
into claude.ai web, Claude Desktop, Claude Code, Cursor or any MCP-aware
client and the model gains the **three MCP primitives** (tools, resources,
prompts) over the Pexels REST surface.

Built around the [MCP spec 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25)
and Anthropic's [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
guidance: strict Pydantic input schemas, structured tool output via
`structuredContent` + `outputSchema`, `isError=true` on tool failure per
SEP-1303, OAuth 2.1 + RFC 9728 + RFC 7591 DCR + PKCE for the HTTP transport.

## What the agent gets

### 8 tools (model-controlled)

| Tool | Purpose |
|---|---|
| `pexels_search_photos` | Search photos. Filters: `orientation`, `size`, `color`, `locale`, plus post-hoc `min_width` / `min_height` / `aspect_ratio`. |
| `pexels_get_photo` | Fetch one photo by id. |
| `pexels_search_videos` | Search videos. Same filters minus `color`. |
| `pexels_get_video` | Fetch one video by id. |
| `pexels_get_collection_media` | Read photos + videos in a Pexels collection. |
| `pexels_get_curated_photos` | Pexels' editor-curated daily photo feed. Post-hoc dim/aspect filters. |
| `pexels_get_popular_videos` | Trending video feed. Native `min_width` / `min_height` / `min_duration` / `max_duration` (Pexels-side), post-hoc `aspect_ratio`. |
| `pexels_get_featured_collections` | Discover curated collection ids (metadata only — pipe an id into `pexels_get_collection_media`). |

### 3 resources (app-controlled, URI templates)

| URI template | MIME | Body |
|---|---|---|
| `pexels://photo/{photo_id}` | `application/json` | `SinglePhotoResult` |
| `pexels://video/{video_id}` | `application/json` | `SingleVideoResult` |
| `pexels://collection/{collection_id}` | `application/json` | `CollectionMediaResult` |

A user pasting a `pexels.com` URL into a chat lets the host attach the
content directly without the agent invoking a tool.

### 2 prompts (user-controlled, claude.ai connector menu)

| Prompt | Arguments | Use case |
|---|---|---|
| `find_hero_image` | `topic`, `orientation?`, `brand_color?`, `aspect_ratio?` | Marketing hero with brand fit |
| `find_broll` | `topic`, `orientation?`, `resolution?`, `aspect_ratio?` | B-roll, reels, hero loops |

Each prompt renders a short user-message brief that names the tool, the
filters and the attribution requirement — the agent acts in one turn
instead of asking the user for parameters.

## Token economy

Every byte that goes onto the wire was audited. Cumulative gains vs the
SDK defaults:

- **Tool descriptions** trimmed to the minimum LLM-actionable signal
  (USE WHEN / DO NOT USE / filters / return shape).
- **Type docstrings** removed from `MediaSize`, `PhotoProjection`,
  `VideoProjection`, `FilterDiagnostics` etc.: they leaked as `description`
  fields into every tool's `$defs`, duplicated across all tools that
  referenced them. Now Python comments only.
- **`serverInfo.instructions`** reduced to one sentence (the attribution
  requirement); the tool list is already shipped by `tools/list`.
- **SDK patch** (see [`_sdk_patches.py`](src/pexels_mcp_server/_sdk_patches.py)):
  - Forces `model_dump(exclude_unset=True)` so unset optional TypedDict
    fields don't leak as `"field": null`.
  - Replaces the SDK's duplicate-content behaviour: instead of shipping
    the payload **twice** (once as `structuredContent`, once as
    indented JSON in `content[]`), tools now ship the structured payload
    plus a 45-char marker in `content[]` pointing at it. Saves ~1500
    tokens per tool call on a 15-photo search.

Numbers for a typical 15-photo search call:

|  | content text | structuredContent | total |
|---|---|---|---|
| SDK default | 7 100c (indented dup) | 5 400c | 12 500c (~3 100 tok) |
| This server | 45c (marker) | 5 400c | **5 450c (~1 360 tok)** |

## How the agent picks the best image

Pexels already ranks results by relevance. The tools just let the agent
narrow the field in one shot:

1. **Frame query + filters** — `orientation` for hero banners,
   `aspect_ratio` for fixed-frame (Instagram 1:1, Story 9:16, hero 16:9),
   `min_width` / `min_height` for hard pixel floors (~4000 for A4 print,
   ~1920 for hero), `color` for brand fit.
2. **Read alt text** — `pexels_search_photos` returns up to 15 candidates
   by default with `alt` text, dimensions and photographer credit. The
   agent drops anything off-topic and returns the best `image_url` plus
   the mandatory `photographer` / `photographer_url`.

When a post-hoc filter (`aspect_ratio` etc.) wipes the page, the envelope
carries a `filter_diagnostics` block telling the agent how to retry.

## Deployment

Designed for **one hosted HTTPS endpoint** with OAuth 2.1 + RFC 9728.
Stdio is supported for local power-user clients (Cursor, scripts).

### Auth model — bring-your-own-key (BYOK) during the OAuth flow

The Python process is both the Resource Server (holding `/mcp`) and the
Authorization Server. The MCP Python SDK mounts every well-known endpoint
automatically: `/.well-known/oauth-protected-resource` (RFC 9728),
`/.well-known/oauth-authorization-server` (RFC 8414), `/authorize`,
`/token`, `/register` (RFC 7591 DCR), all with PKCE.

`register_client` rejects `redirect_uri` schemes that aren't `https://`
or `http://` loopback (OAuth 2.1 phishing mitigation).

After the standard handshake, the server redirects the user's browser to
`/setup`, a short HTML form asking for a Pexels API key. The user pastes
their free key (from <https://www.pexels.com/api/>), the server validates
it against `api.pexels.com`, then mints the OAuth code with the key bound
to the soon-to-be-issued access token (30-day TTL). Every tool / resource
call resolves the caller's key by Bearer-token lookup.

For per-request clients (Cursor stdio bridges, scripts), the server also
accepts an `X-Pexels-Api-Key` HTTP header as a fallback.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `TRANSPORT` | yes | `streamable-http` or `stdio` (default). |
| `MCP_SERVER_URL` | yes (HTTP) | Public HTTPS URL of this service. No trailing slash. |
| `MCP_ALLOWED_HOSTS` | no | Comma-separated `Host` allowlist (DNS rebinding protection). Auto-set to `MCP_SERVER_URL`'s hostname if unset. |
| `MCP_RATE_LIMIT_PER_MINUTE` | no (60) | Per-IP rate limit. `/healthz`, `/readyz`, OAuth metadata are exempt. |
| `MCP_TRUSTED_PROXY_HOPS` | no (1) | Proxies in front of the app (Koyeb LB = 1, Cloudflare-then-Koyeb = 2, no proxy = 0). |
| `REDIS_URL` | no | When set, OAuth state lives in Redis and survives restarts. Supports `rediss://` (TLS). |
| `MCP_ENCRYPTION_KEY` | yes if `REDIS_URL` | 32-byte url-safe base64 Fernet key. Pexels keys are encrypted at rest. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `HOST` / `PORT` | no | Default `127.0.0.1:8000`. Docker flips host to `0.0.0.0`. |
| `LOG_LEVEL` | no (`INFO`) | Standard Python levels. |
| `LOG_FORMAT` | no | `json` (default in HTTP) or `text` (default in stdio). |
| `PEXELS_API_KEY` | stdio only | Default key for local clients. Ignored in HTTP mode. |

### Persistent sessions (Redis, optional but recommended in prod)

Without `REDIS_URL`, OAuth state is in-memory and every Koyeb deploy
forces users to re-walk `/setup`. With Redis, sessions survive restarts.
The bound Pexels key is encrypted client-side with Fernet (AES-128-CBC +
HMAC-SHA256) before being written — a leaked Redis dump alone yields
opaque ciphertext.

Compatible providers: [Upstash Redis](https://upstash.com/) (free tier
10k cmd/day, 256 MB, TLS), Redis Cloud, self-hosted. See
[`docker-compose.yml`](docker-compose.yml) for the local dev setup.

### Koyeb (one-command deploy)

```bash
koyeb service create pexels-mcp \
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

Then add `REDIS_URL` + `MCP_ENCRYPTION_KEY` for persistent sessions.

### Smoke test

```bash
URL=https://<your-service>.koyeb.app
curl -s "$URL/healthz"   # -> ok
curl -s "$URL/.well-known/oauth-protected-resource" | head -20
curl -i -X POST "$URL/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json,text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-11-25' \
  -d '{}' | head -10
# -> 401 with WWW-Authenticate: Bearer ... resource_metadata="..."
```

### Connect a client

| Client | Steps |
|---|---|
| **claude.ai web** | Settings → Connectors → Add custom connector → URL `https://<host>/mcp`. Click *Connect*. Paste your Pexels key on the `/setup` page. |
| **Claude Desktop** | Settings → Connectors → Add (remote) → same URL. Same `/setup` flow. |
| **Claude Code** | `claude mcp add pexels --transport http https://<host>/mcp`. |
| **MCP Inspector** | `npx @modelcontextprotocol/inspector` → paste the URL. |

## Local development

```bash
git clone https://github.com/VictorNain26/pexels-mcp-server
cd pexels-mcp-server
uv sync --all-extras
```

### HTTP server (prod parity)

```bash
TRANSPORT=streamable-http HOST=127.0.0.1 PORT=8000 \
  MCP_SERVER_URL=http://127.0.0.1:8000 \
  uv run pexels-mcp-server
```

### Full stack with Redis (Fernet path exercised)

```bash
echo "MCP_ENCRYPTION_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" > .env
docker compose up --build
```

### Stdio (Cursor, local scripts)

```bash
PEXELS_API_KEY=your-key uv run pexels-mcp-server
```

Stdio bypasses OAuth — the key comes from the env var directly.

### Check suite

```bash
uv run ruff check && uv run ruff format --check
uv run mypy src
uv run python -m pytest
```

## Response shape

`pexels_search_photos(query="paris", per_page=1)` ships:

- `structuredContent` (canonical payload, machine-readable, ~600c):

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
      "alt": "Vibrant street view of central Paris ...",
      "page_url": "https://www.pexels.com/photo/.../28448939/",
      "photographer": "Sergey Guk",
      "photographer_url": "https://www.pexels.com/@sergeyguk",
      "width": 4000,
      "height": 6000,
      "image_url": "https://images.pexels.com/photos/28448939/.../original.jpeg"
    }
  ]
}
```

- `content[0]` (45-char marker): `"See structuredContent for the result payload."`

The marker exists so backwards-compat clients reading `content` see a
non-empty block. Modern clients (claude.ai web, Claude Desktop, MCP
Inspector 0.10+) consume `structuredContent` directly.

## Three usage examples

### 1. Hero image with brand color and aspect ratio

```python
pexels_search_photos(
  query="modern open-plan office workspace",
  orientation="landscape",
  size="large",
  color="blue",
  aspect_ratio="16:9",
  min_width=1920,
  per_page=6,
)
```

### 2. 4K B-roll, fixed aspect

```python
pexels_search_videos(
  query="aerial drone shot of mountain lake at dawn",
  orientation="landscape",
  size="large",
  aspect_ratio="16:9",
  per_page=10,
)
```

`video_url` is the direct MP4 of the top-resolution variant.

### 3. Drill into a Pexels collection

```python
pexels_get_collection_media(collection_id="9j5dhpu", per_page=20)
```

The response splits `photos[]` and `videos[]`. Filter to one type with
`type="photos"` or `type="videos"`.

## Rate limits and attribution

Pexels free tier: **200 requests/hour, 20 000 requests/month** on the
caller's key (per Pexels' [API docs](https://www.pexels.com/api/documentation/)).
The server warns to stderr below 100 remaining; the response envelope
does not carry rate-limit metadata (saves tokens — flip `LOG_LEVEL=DEBUG`
if you need it).

If you publish anything returned by this server you **must** credit the
photographer / videographer and link back to Pexels per the
[Pexels licence](https://www.pexels.com/license/). Every tool, resource
and prompt is shaped so the LLM sees `photographer` / `uploader_name`
and matching URLs and can surface them in the user-facing answer.

## Architecture notes

- **3-of-3 MCP primitives.** Tools (model-controlled), Resources
  (app-controlled, URI templates per RFC 6570), Prompts (user-controlled,
  surfaced in claude.ai's connector menu).
- **Spec-compliant auth.** OAuth 2.1 Resource Server + Authorization
  Server in one process via the MCP Python SDK's
  `OAuthAuthorizationServerProvider`. RFC 9728 PRM, RFC 8414 ASM, RFC 7591
  DCR, PKCE — all served by the SDK. The only custom routes are
  `GET /` (landing) and `GET/POST /setup` (BYOK form).
- **Stateless HTTP by default.** `stateless_http=True, json_response=True`
  so deployment scales horizontally without sticky sessions. Trade-off:
  no sampling / no `ctx.report_progress` / no resource subscriptions —
  documented in [`CLAUDE.md`](CLAUDE.md).
- **Read-only by construction.** Every tool advertises
  `readOnlyHint=true, destructiveHint=false, idempotentHint=true,
  openWorldHint=true` plus a `title`.
- **Structured tool output + `isError=true`.** Tools return a `TypedDict`;
  the SDK auto-generates `outputSchema`. Errors raise → FastMCP wraps in
  `CallToolResult(isError=true)` per SEP-1303.
- **Strict inputs.** Pydantic v2 with `extra="forbid"`; invalid values
  come back as `Invalid parameters: <field>: <reason>`.
- **Token-lean payloads.** See the [Token economy](#token-economy)
  section above.
- **SDK patches** in [`_sdk_patches.py`](src/pexels_mcp_server/_sdk_patches.py).
  The only place in the repo that mutates third-party state.

## Health and probes

`GET /healthz` (liveness) and `GET /readyz` (readiness) return `200 ok`
and bypass auth. The Dockerfile declares `HEALTHCHECK` against `/healthz`.

## Compatibility

- Python 3.10, 3.11, 3.12.
- `mcp` SDK pinned `>=1.25,<2`.
- Transport: stdio + Streamable HTTP. Legacy SSE is not enabled.
- MCP spec 2025-11-25 (SDK negotiates downgrade to 2025-06-18 / 2025-03-26).

See [SECURITY.md](.github/SECURITY.md) to report a vulnerability,
[PRIVACY.md](PRIVACY.md) for what the server does and doesn't store.

## License

MIT. See [LICENSE](LICENSE).
