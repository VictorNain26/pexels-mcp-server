# Privacy Policy

_Last updated: 2026-05-20. Effective for `pexels-mcp-server` v0.6.0 and later._

`pexels-mcp-server` is a Model Context Protocol (MCP) server that proxies
read-only requests to the public [Pexels REST API](https://www.pexels.com/api/).
This document explains what data the server sees, what it does with that data,
and what it does **not** do.

## 1. What the server processes

The server exposes the three MCP primitives — tools, resources, prompts
(see [README](README.md)). All three are read-only.

For every call to a **tool** or a **resource**, the server receives — for the
duration of one request only:

- The **call arguments**: a search query, photo / video id, collection id,
  pagination, filters, locale.
- The caller's **Pexels API key**, either bound to the access token during
  the BYOK `/setup` step, supplied per-request via the `X-Pexels-Api-Key`
  header (Streamable HTTP), or read from `PEXELS_API_KEY` (stdio only).
- The **Bearer access token** issued by the server's own `/token`
  endpoint. Opaque random string, no user identity — only a proof that
  the client walked the MCP OAuth 2.1 handshake.

For **prompts**, no Pexels call is made and no API key is read: prompts are
pure template renderers that return a brief string to the agent. The server
sees only the prompt arguments (e.g. a topic string, a brand color).

The server forwards the search / lookup parameters and the caller's Pexels
API key to `https://api.pexels.com`. The Pexels response is projected into
a token-lean JSON envelope and returned to the MCP client.

## 2. What the server stores

The persistence posture depends on the operator's deployment configuration.

### 2.a. Default (in-memory, `REDIS_URL` unset)

**Nothing on disk.** No database, no on-disk cache, no session store:

- A process restart drops every Bearer token, every authorization code,
  every registered OAuth client, and every bound Pexels key — clients
  re-auth transparently on the next call.
- The Pexels API key bound during the OAuth setup lives in process memory
  for the access token's 30-day lifetime and is then released. When
  supplied via the `X-Pexels-Api-Key` header it is read from the request
  scope on every call and never retained beyond the request.
- Bearer tokens issued by `/token` live in process memory only and are
  never logged.

### 2.b. Persistent (Redis-backed, `REDIS_URL` set)

The operator opts into persistent OAuth state by setting `REDIS_URL` (and
`MCP_ENCRYPTION_KEY`). In this mode three pieces of state live in Redis
under the `mcp:` namespace:

- `mcp:client:<client_id>` — DCR client metadata (no PII, just redirect
  URIs and OAuth client info).
- `mcp:token:<access_token>` — issued access token (client_id, scopes,
  expiry, audience). 30-day TTL.
- `mcp:pexels:<access_token>` — the bound Pexels API key, **encrypted at
  rest with Fernet (AES-128-CBC + HMAC-SHA256)** using the operator's
  `MCP_ENCRYPTION_KEY`. 30-day TTL.

A leaked Redis snapshot alone is **not** enough to recover any user's
Pexels API key — the operator's `MCP_ENCRYPTION_KEY` env var is the second
factor. Rotating it invalidates every stored binding gracefully (users
transparently re-walk `/setup`).

Short-lived state (authorization codes, pending `/setup` sessions, the
transient code→key binding) always lives in process memory regardless of
backend — their 5-minute and 15-minute TTLs make persistence
counterproductive.

### 2.c. Always — never persisted

- **Tool / resource arguments and Pexels responses** live in process
  memory for the duration of a single request and are then released.
  They are never written to Redis, disk, or a log line.
- **Prompt arguments** are template inputs only; the server never
  forwards them anywhere and never persists them.
- **The `X-Pexels-Api-Key` request header** (fallback path) is read from
  the request scope on every call and never retained.

The Streamable HTTP transport runs with `stateless_http=True`, so there is
no MCP session identifier allocated server-side either.

## 3. What the server logs

The server writes structured logs to **stderr only** (stdout is reserved for
the JSON-RPC stream). The default log level is `INFO` and can be tuned with
the `LOG_LEVEL` environment variable. In HTTP mode logs are emitted as JSON,
one record per line, for log-drain ingestion. In stdio mode logs are plain
text.

The following events are logged at `INFO`:

- Server startup (transport, host, port, whether Bearer auth is enabled).
- Pexels client lifecycle (boot, close).

At `WARNING`:

- A rejected unauthenticated request to `/mcp`, with the remote IP (no port,
  no payload, no headers).
- A Pexels rate-limit reading below 100 requests remaining.
- A Pexels 5xx triggering a single retry.

The server **never** logs:

- The Pexels API key (header or env var).
- The Bearer token.
- Tool / resource / prompt arguments.
- Pexels response bodies.

## 4. Third parties

The server makes outbound HTTPS calls to:

- `api.pexels.com` — the Pexels REST API, with the caller's Pexels API key
  in the `Authorization` header. Pexels' privacy practices are governed by
  the [Pexels Privacy Policy](https://www.pexels.com/privacy-policy/).
- **The Redis endpoint configured via `REDIS_URL`** (when set) — encrypted
  Pexels-key ciphertext + access-token metadata over TLS (`rediss://`).
  Pick a provider whose privacy and data-residency story matches your
  threat model: [Upstash](https://upstash.com/trust/privacy), [Redis
  Cloud](https://redis.com/legal/privacy-policy/), or self-hosted (in
  which case the operator is the data processor).

No other outbound calls are made. The server **does not** fetch thumbnails
or any binary content — it only returns the `image_url` / `video_url`
references and the user's MCP client (or browser) loads them directly from
the Pexels CDN if displayed. There is no telemetry beacon, no analytics
SDK, no metrics endpoint, no remote configuration fetch.

## 5. Hosted deployments (Koyeb, Fly, Cloud Run, …)

Operators who deploy the server on a hosted platform are responsible for the
privacy posture of that platform's log drain, network observability and
backups. The server itself does not produce any per-user persistent artifact
that would survive a process restart.

The recommended deployment posture is multi-tenant: each MCP client sends
its own `X-Pexels-Api-Key` header so the host never holds a shared key. In
that configuration the operator pays Koyeb / Fly bills only; every caller
pays their own Pexels quota.

## 6. Retention

- **In-memory mode** (`REDIS_URL` unset): zero. Wiped on every process restart.
- **Redis-backed mode** (`REDIS_URL` set): up to 30 days for access tokens
  + bound Pexels keys, refreshed on each token re-issuance. The operator
  controls retention by tuning Redis maxmemory + eviction policy; the
  server itself never extends the TTL beyond the access-token lifetime.

## 7. Children

The server has no notion of "users" and does no age gating. The Pexels API
is the source of truth for content moderation.

## 8. Changes to this policy

Changes are tracked in [CHANGELOG.md](CHANGELOG.md) under the version that
ships them. The current version's policy is the file in the repo at the
matching tag.

## 9. Contact

For privacy-specific questions or to report a suspected leak, follow the
private disclosure process in [SECURITY.md](.github/SECURITY.md).
