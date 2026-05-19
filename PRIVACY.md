# Privacy Policy

_Last updated: 2026-05-19. Effective for `pexels-mcp-server` v0.6.0 and later._

`pexels-mcp-server` is a Model Context Protocol (MCP) server that proxies
read-only requests to the public [Pexels REST API](https://www.pexels.com/api/).
This document explains what data the server sees, what it does with that data,
and what it does **not** do.

## 1. What the server processes

When an MCP client calls one of the tools the server exposes (see [README](README.md))
the server receives, for the duration of a single request:

- The **tool arguments** sent by the agent (search query, photo or video id,
  pagination, filters, locale, etc.).
- The caller's **Pexels API key**, either submitted once during the OAuth
  setup step and bound server-side to the issued access token, or sent
  per-request as an `X-Pexels-Api-Key` HTTP header (Streamable HTTP
  transport), or read from the `PEXELS_API_KEY` environment variable
  (stdio transport only).
- The **Bearer access token** issued by the server's own `/token` endpoint
  (Streamable HTTP transport). The token is an opaque random string held
  in process memory; it is not a user identity, only a proof that the
  client walked through the spec-mandated OAuth handshake.

The server forwards the search/lookup parameters and the caller's Pexels API
key to `https://api.pexels.com`. The Pexels response is then projected into
a token-lean JSON envelope and returned to the MCP client.

## 2. What the server stores

**Nothing on disk.** The server has no database, no on-disk cache, no
session store:

- No file or database holds user-supplied data across restarts. A process
  restart drops every Bearer token, every authorization code, every
  registered OAuth client, and every bound Pexels key — clients re-auth
  transparently on the next call.
- The Pexels API key, when supplied via the OAuth setup step, lives in
  process memory bound to the access token for the token's 1-hour lifetime
  and is then released. When supplied via the `X-Pexels-Api-Key` header it
  is read from the request scope on every call and never retained beyond
  the request. There is no per-user key vault, no log entry that contains
  the key.
- Bearer tokens issued by `/token` live in process memory only, expire after
  1 hour, and are never logged.
- Tool arguments and Pexels responses live in process memory for the
  duration of a single request and are then released.

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
- Tool arguments.
- Pexels response bodies.

## 4. Third parties

The server makes outbound HTTPS calls to:

- `api.pexels.com` — the Pexels REST API, with the caller's Pexels API key
  in the `Authorization` header. Pexels' privacy practices are governed by
  the [Pexels Privacy Policy](https://www.pexels.com/privacy-policy/).

No other outbound calls are made. There is no telemetry beacon, no analytics
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

Zero. See section 2.

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
