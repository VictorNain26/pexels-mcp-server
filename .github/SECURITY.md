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

This server holds a `PEXELS_API_KEY` in the process environment and forwards search/lookup requests to `api.pexels.com`. Specifically be aware of:

- **Stdio transport.** Anything that can spawn the server inherits the configured environment, including `PEXELS_API_KEY`. Treat the key like any other deployment secret.
- **Streamable HTTP transport.** The HTTP endpoint speaks raw MCP. Put it behind authentication (Bearer header, mTLS, your platform's auth) before exposing it publicly, otherwise anyone hitting `/mcp` consumes your Pexels quota.
- **Preview tool.** `pexels_preview_media` only accepts URLs whose host is `images.pexels.com`. SSRF attempts against internal hosts are rejected at validation time. If you find a bypass, report it.

Out of scope: anything that requires already-compromised credentials, social-engineering an operator into setting an attacker-controlled `PEXELS_API_KEY`, or attacks on third-party MCP clients.
