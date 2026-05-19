# Submitting to the Anthropic Connector Directory

This document tracks the submission of `pexels-mcp-server` to the
[Anthropic Connector Directory](https://www.anthropic.com/partners/mcp).
It is **not** part of the runtime; it lives in the repo so the next person
who reviews the submission status has the full picture.

## Submission URL

Anthropic accepts remote-MCP submissions via the official form:

> **[https://clau.de/mcp-directory-submission](https://clau.de/mcp-directory-submission)**

(For firewall-restricted environments, escalate to `mcp-review@anthropic.com`.)

Submissions are reviewed in roughly **two weeks**; the form is always open
but proactive notifications are not guaranteed. A self-serve status dashboard
is rolling out on claude.ai.

## What to put in the form

Anthropic's [submission docs](https://claude.com/docs/connectors/building/submission)
list nine sections. Pre-filled answers for this repo:

| Field | Value |
|---|---|
| **Server name** | `Pexels` |
| **Public URL** | `https://pexels-mcp-tomia-9e578486.koyeb.app/mcp` (or the future custom domain) |
| **Tagline** | "Free Pexels photo and video search inside Claude." |
| **Description** | Async MCP server that exposes five read-only Pexels REST tools (search photos / get photo / search videos / get video / get collection media). Per-user BYOK OAuth 2.1 — each caller pastes their own Pexels API key once into the `/setup` form during the OAuth handshake, so quota stays per-user. |
| **Transport** | Streamable HTTP (per MCP spec 2025-11-25). |
| **Auth type** | OAuth 2.1 + RFC 9728 PRM + RFC 8414 AS metadata + RFC 7591 DCR + PKCE. BYOK flow: the server redirects to its own `/setup` HTML form during `/authorize`, asks for the user's Pexels API key, validates it against `api.pexels.com`, then mints the authorization code with the key bound to the future access token. |
| **Tools** | 5 read-only tools, see [README](README.md#what-the-agent-can-do). Every tool advertises `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=true`, has a `title`, and returns structured JSON output (`structuredContent` + serialized text). |
| **Test credentials** | A free Pexels API key (https://www.pexels.com/api/) — Anthropic reviewers can sign up in <2 min. Provide one in the form's "test account" field; the key is the user's own Pexels key, not a shared secret. |
| **Public docs** | This repo: <https://github.com/VictorNain26/pexels-mcp-server>. README covers connect flow + three usage examples. |
| **Privacy policy** | [`PRIVACY.md`](PRIVACY.md) in this repo. Section-1 "what the server processes", section-2 "what the server stores (nothing)", section-3 "what the server logs (no payloads, no keys)", section-4 "third parties (api.pexels.com, images.pexels.com only)". |
| **Support channel** | GitHub issues: <https://github.com/VictorNain26/pexels-mcp-server/issues> |
| **GA date** | The version tagged on `main` at submission time (current: v0.6.0+ unreleased). Pin a release tag before submitting. |

## Pre-submission checklist

This is the list of things to verify before clicking *Submit*. Most are
already in place — the items marked `[ ]` need attention.

### Security & technical (Anthropic-mandated)

- [x] OAuth 2.0+ implemented (we ship OAuth 2.1 with PKCE).
- [x] Every tool has `readOnlyHint` and a `title` (set in `server.py` via
      `ToolAnnotations`). All five tools also return structured output
      (`structuredContent`) as recommended by the 2025-11-25 spec.
- [x] `Origin` header validation available (`MCP_ALLOWED_HOSTS` env var,
      set to `{{ KOYEB_PUBLIC_DOMAIN }}` in production).
- [x] HTTPS public endpoint (Koyeb terminates TLS for us).
- [x] Spec-compliant `WWW-Authenticate: Bearer ... resource_metadata=...`
      on 401 responses.

### Documentation

- [x] Public README with install + usage + connect-flow steps.
- [x] Three concrete usage examples in the README.
- [x] Architecture & contributing docs (`CONTRIBUTING.md`, `CLAUDE.md`).
- [x] Per-tool LLM-facing docstrings (USE WHEN / DO NOT USE WHEN / return
      shape).

### Privacy

- [x] `PRIVACY.md` published at the repo root, covering:
      data collection, usage, storage (none), third-party sharing
      (api.pexels.com, images.pexels.com), retention (zero), contact info.
- [x] No persistent storage of user keys, tokens or payloads.

### Branding assets

- [ ] **Server logo** — PNG or SVG, square. To be added at
      `.github/branding/logo.png` and linked from the README / submission form.
- [ ] **Favicon** — same image scaled down. Optional but checked by reviewers.
- [ ] **3-5 screenshots** of "app response only" (Pexels MCP results in a
      Claude conversation, without the user prompt visible).
- [ ] **Carousel artifacts** using the
      [Anthropic MCP Apps Figma community file](https://www.figma.com/) (template).

### Final knobs

- [ ] Tag a release on `main` (`vX.Y.Z`) so the submission references an
      immutable version.
- [ ] Decide on a stable production URL (the Koyeb default
      `pexels-mcp-tomia-9e578486.koyeb.app` works; a custom domain is
      stronger long-term — see [issue: custom domain](https://github.com/VictorNain26/pexels-mcp-server/issues)).
- [ ] Bump `MCP_RATE_LIMIT_PER_MINUTE` if the expected post-listing traffic
      is heavier than 60/min/IP.

## Common rejection reasons to avoid

Per the [Sunpeak Connector Directory submission walkthrough](https://sunpeak.ai/blogs/claude-connector-directory-submission/) (2026):

| Reason | % of rejections | How we address it |
|---|---|---|
| Missing tool annotations | ~30 % | All 5 tools advertise the full `ToolAnnotations` block. |
| OAuth callback URL errors | next-most-common | We accept whatever `redirect_uri` the client registers via DCR — claude.ai's callback domain is included automatically. |
| Missing privacy policy | rejection on the spot | [`PRIVACY.md`](PRIVACY.md) covers every required section. |
| Incomplete docs | common | README + CLAUDE.md + CONTRIBUTING + this file + three usage examples. |
| "Still in beta" | common | The first submission must be on a tagged release, not a `[Unreleased]` HEAD. |

## After submission

- Watch `mcp-review@anthropic.com` for follow-ups.
- Treat any feedback as a code review: open issues / PRs in this repo for
  each item the reviewer flags so the changes are auditable.
- Once listed, monitor traffic on Koyeb (`koyeb service logs ...`) and the
  rate-limit warnings (`MCP_RATE_LIMIT_PER_MINUTE` knob).
