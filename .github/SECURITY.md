# Security Policy

## Supported versions

Only the latest release line receives security updates.

| Version | Supported |
|---|---|
| 0.x | yes |

## Reporting a vulnerability

Please **do not open a public GitHub issue** for security problems.

Use one of these channels instead:

1. **Private vulnerability report** via [GitHub Security Advisories](https://github.com/VictorNain26/pexels-mcp-server/security/advisories/new). This is the preferred path.
2. **Email** to `victor.lenain26@gmail.com` with the subject `[SECURITY] pexels-mcp-server`.

Please include:

- A short description of the issue and its impact.
- Steps to reproduce (commands, payloads, environment).
- The commit SHA or version where you observed the issue.

You will get an acknowledgement within 72 hours. Fixes for confirmed issues are released as patch versions and credited in [CHANGELOG.md](../CHANGELOG.md) unless you ask to stay anonymous.

## Threat model notes

This server forwards search/lookup requests to `api.pexels.com` using a Pexels API key supplied by the caller. The server itself does not hold a shared Pexels key in production. Specifically be aware of:

- **Stdio transport.** Anything that can spawn the server inherits the configured environment, including `PEXELS_API_KEY` if the operator sets it. Treat the key like any other deployment secret. Stdio is a local-only transport — exposing it over a network is out of scope.
- **Streamable HTTP transport.** The HTTP endpoint is OAuth 2.1-protected (RFC 9728 + RFC 8414 + RFC 7591 DCR + PKCE, served by the MCP Python SDK). Anonymous requests to `/mcp` return 401 with a spec-compliant `WWW-Authenticate` header. Each caller supplies their own Pexels API key during the OAuth setup step (bound to their access token) or as an `X-Pexels-Api-Key` header per request, so the server never holds a shared key and an attacker cannot drain anyone else's Pexels quota.
- **Rate limiting.** The middleware applies a per-source-IP cap (default 60 req/min, tunable via `MCP_RATE_LIMIT_PER_MINUTE`). The real client IP is read from `X-Forwarded-For` using a configurable trusted-proxy hop count (`MCP_TRUSTED_PROXY_HOPS`, default 1 for Koyeb's LB) so a client cannot spoof the IP by injecting a leftmost entry. Health probes and `.well-known/oauth-*` endpoints are exempt.
- **DNS rebinding protection.** Auto-enabled in HTTP mode: the hostname of `MCP_SERVER_URL` is added to the allowed-hosts list. Extend with `MCP_ALLOWED_HOSTS` if you front the service with multiple hosts.

Out of scope: anything that requires already-compromised credentials, social-engineering an operator into setting an attacker-controlled `PEXELS_API_KEY` in a stdio deployment, or attacks on third-party MCP clients.
