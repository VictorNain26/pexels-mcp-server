# CLAUDE.md

Operational guide for AI coding sessions in this repo. Read this **first**, then
load the functional context from the docs listed below.

## Read the functional context, do not duplicate it here

For *what* this server does and *how* it is wired, consult these — they are the
single source of truth:

| Doc | Purpose |
|---|---|
| [`README.md`](README.md) | User-facing overview, tool table, OAuth flow, deployment guide. |
| [`PRIVACY.md`](PRIVACY.md) | What the server processes, stores, logs, and forwards. |
| [`CHANGELOG.md`](CHANGELOG.md) | History of breaking and behaviour-affecting changes. |

If something is documented there, **link** from your work — do not paste a
re-explanation into this file or into a code comment.

## Code conventions in this repo

> These are guidelines, not laws. If a rule below blocks a clearly better
> approach, override it and document the reason in the PR description.
> Don't keep a bad practice just to honour the letter of this file.

- **Match scope.** A bug fix touches the bug. A refactor goes in a separate PR.
- **YAGNI.** Three similar lines beat a premature abstraction. No feature flags
  or backwards-compat shims when the code can simply change.
- **Trust internal, validate at boundaries.** Pydantic models gate every tool
  argument (`ConfigDict(extra="forbid")`); internal helpers do not re-check.
- **Comments answer the *why*, never the *what*.** No "added for ticket X",
  no "used by Y" — those rot. Source of truth for *why* is the commit
  message and the linked spec/RFC.
- **Errors are agent-actionable, raised from the tool body.** Validation
  errors flatten via `server.py::_raise_invalid_params`; Pexels errors
  (`PexelsAuthError`, `PexelsRateLimitError`, `PexelsAPIError`) propagate
  as-is. FastMCP catches each and marks the `CallToolResult` with
  `isError=true` per MCP spec 2025-11-25 SEP-1303 — never catch and
  return a string, that would silently mark the result as success.
- **Tool docstrings are written for the LLM caller**, not the human dev.
  Every tool MUST have: a one-line purpose, **USE WHEN**, **DO NOT USE
  WHEN**, filter recap, return-shape teaser. Follow
  [Anthropic's "Writing tools for agents"](https://www.anthropic.com/engineering/writing-tools-for-agents).
  Keep them tight — every char ships in `tools/list` on every
  conversation init.
- **Tool annotations are not optional.** Every `@mcp.tool` carries
  `ToolAnnotations` with `title` + `readOnlyHint`. The Anthropic Connector
  Directory rejects ~30 % of submissions for missing these.
- **Three MCP primitives, all read-only.** This server ships the full
  set: 8 tools (model-controlled), 3 resources (URI templates per RFC
  6570), 2 prompts (claude.ai connector menu). See `README.md` for the
  surface. Future primitives go through the same trim discipline — any
  type-level docstring leaks into `$defs` of every referencing schema.
- **No hand-rolled OAuth, no hand-rolled bearer.** The MCP SDK owns the auth
  surface. The only custom piece is `auth.py`'s `OAuthAuthorizationServerProvider`
  implementation and the public landing page at `GET /` registered via
  `@mcp.custom_route`.
- **No outbound URL fetching from tools.** Removing the `pexels_preview_media`
  tool also removed the SSRF surface. Future tools that take URLs MUST
  allowlist hosts at the Pydantic layer; document the threat model.
- **Imports stay narrow.** `mcp`, `httpx`, `pydantic`, `uvicorn`, `redis`,
  `cryptography` are the only runtime deps. The last two are gated behind
  `REDIS_URL` — the import path in `storage.py` is lazy so a stdio install
  never pays the import cost. Anything new needs a justification in the PR.

## Conscious trade-offs (NOT bugs)

These are deliberate design choices. Do **not** "fix" them without
reading the rationale first.

- **`stateless_http=True` + `json_response=True`** on the FastMCP instance.
  Per the MCP Advanced Topics course, this disables sampling, the
  long-lived GET SSE channel, `ctx.report_progress()`, `ctx.info()`,
  and resource subscriptions. We accept that to scale Koyeb horizontally
  with no sticky sessions. **If you ever need notifications back to the
  client, flip both flags and accept the sticky-session cost.**
- **Single-replica only.** The in-process rate limiter
  (`transport.py::_SlidingWindowLimiter`), the OAuth provider's pending
  setups, the auth codes, and the code→key transitional binding all live
  in module-local dicts (`auth.py::_pending_setups`, `_auth_codes`,
  `_code_to_key`). A user who lands on replica B after `/setup` on
  replica A gets a 404. The persistent `TokenStore` covers DCR clients +
  access tokens + bound Pexels keys; everything else is intentionally
  process-local. Scaling out requires moving those three dicts into
  Redis too — not done because we are well under one-replica capacity.
- **No refresh tokens.** Clients re-walk OAuth on access-token expiry.
  Smaller wire surface, one less code path to maintain.
- **`max_per_minute=60` rate limit per IP, not per token.** A token-bound
  cap would require sticky sessions or a Redis counter. Per-IP is
  sufficient for the eco-nano tier and avoids both.
- **SDK monkey-patches in `_sdk_patches.py`** — the only place in the
  repo allowed to mutate third-party state. Two patches today:
  1. `FuncMetadata.convert_result` is forced to pass `exclude_unset=True`
     to `model_dump`. Without it, optional TypedDict fields leak as
     `"field": null` and the strict `outputSchema` rejects every call
     with `"None is not of type 'object'"`.
  2. Tool result text content is serialised as **compact JSON**
     (`separators=(",", ":")`) instead of the SDK's
     `pydantic_core.to_json(indent=2)`. Saves ~30 % bytes per call
     while keeping the agent able to read the payload from `content`
     — claude.ai's custom-connector path (May 2026) still feeds only
     `content` to the model, so a marker-only `content` made the
     agent hallucinate CDN patterns. When a future Claude release
     confirms native `structuredContent` consumption for custom
     connectors, switch back to a marker and document the
     verification.

## Day-to-day commands

```bash
uv sync --all-extras          # one-time install
uv run ruff check             # lint
uv run ruff format --check    # format
uv run mypy src               # strict type-check
uv run pytest                 # tests
uv run pytest tests/test_auth.py::test_authorize_issues_code  # single test
```

Run the HTTP server locally (parity with prod):

```bash
TRANSPORT=streamable-http HOST=127.0.0.1 PORT=8000 \
  MCP_SERVER_URL=http://127.0.0.1:8000 \
  uv run pexels-mcp-server
```

## AI lifecycle — May 2026 baseline

A productive session in this repo has the same shape every time. Deviating
from this shape is what produces the patches you regret later.

### 1. Frame the work before touching code

- **Read the user message twice.** "Drop the passcode" is not the same as
  "make the OAuth optional" — clarify scope by quoting the message back if
  ambiguous.
- **Doc-first for any non-trivial library use.** Context7 (`query-docs`) or
  WebFetch the published spec / SDK source. Local `node_modules`-style read of
  `.venv/lib/.../<pkg>` is also valid. Never invoke an API from memory if a
  fresh check is one tool call away.
- **For the MCP spec, the canonical sources are:** the
  [2025-11-25 spec](https://modelcontextprotocol.io/specification/2025-11-25)
  (current revision; the SDK still negotiates downgrade to `2025-06-18`
  and `2025-03-26`) and the
  [SDK reference implementation](https://github.com/modelcontextprotocol/python-sdk/tree/main/examples/servers/simple-auth).
  Match their patterns; do not invent variants. Notable 2025-11-25 changes
  we already comply with: structured tool output (`structuredContent`),
  `isError=true` on tool execution failures (SEP-1303), and
  `serverInfo.instructions`.

### 2. Plan, then branch

- **Use TaskCreate** to track multi-step work. Mark `in_progress` *before*
  starting, `completed` only when verified. Stale tasks confuse the user.
- **One branch per coherent change.** `chore/`, `feat/`, `fix/` prefixes per
  conventional commits. Never push direct to `main` (rule from the user-level
  CLAUDE.md).

### 3. Code with tests, lint, types green at every commit

- `uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest`
  must pass locally before each commit.
- **Tests live next to the thing they test**: `tests/test_<module>.py` mirrors
  `src/pexels_mcp_server/<module>.py`. Integration tests of FastMCP wiring go
  in `tests/test_server_config.py`.
- **No `# type: ignore`** without an adjacent comment explaining the SDK
  contract that justifies it.

### 4. Open a PR, let the bots review

- Each PR description has a Summary, a list of changes, and a Test plan
  checkbox list. Reference closed issues with `Closes #N`.
- **CI must be green** (matrix 3.10/3.11/3.12 + docker-build) before merge.
- **CodeRabbit review** is part of the loop. Treat Major findings as
  blocking; Nits get fixed if the diff stays small. Document the rationale
  if you skip one.
- The user's standing rule is **squash-merge** so `main` keeps one commit per
  PR. Delete the branch on merge.

### 5. Production rollout via Koyeb (zero downtime)

- **The Koyeb auto-deploy is push-based** (`auto_release` on `main`). Any merge
  to `main` triggers a build + rolling deploy in <2 min.
- **For env-var changes that the *new* code depends on:** `save_only=true`
  the new env config *first* (no redeploy), then merge the PR — the auto
  redeploy picks up both the new code and the new env in one rolling update.
- **For env-var cleanups** (vars the new code stopped reading): use
  `skip_build=true` with the same image — it's a fast in-place restart, no
  rebuild.
- **Smoke-test after every deploy**, in this order:
  1. `GET /healthz` → `200 ok`
  2. `GET /.well-known/oauth-protected-resource` → valid RFC 9728 JSON
  3. `POST /mcp` without `Authorization` → `401` with
     `WWW-Authenticate: Bearer ... resource_metadata="..."`
- **Watch logs** on the first deploy after a structural change:
  `mcp__koyeb__query-logs` with `service_id` of the `mcp` service. JSON
  format is on by default in HTTP mode, so grep / filter by `level`, `logger`,
  `msg`.

## Repo memory: what *not* to do (lessons from May 2026)

These are the failure modes from the build-out of this server. Future sessions
should not repeat them.

| Anti-pattern | Why it bit us | Right move |
|---|---|---|
| Hand-rolled bearer middleware to "save time" | Broke claude.ai's OAuth discovery; took two PRs to undo. | Use `auth_server_provider` + `AuthSettings` from the SDK from day one. |
| Passing both `auth_server_provider` *and* `token_verifier` | The SDK raises `ValueError` at boot — caught only at docker smoke-test time. | Pass the provider only; the SDK derives the verifier. |
| Adding routes via `starlette_app.routes.append(...)` | Works but bypasses the SDK's public API. | Use `@mcp.custom_route` — its docstring names "OAuth callbacks" as the intended use. |
| Inlining 100 lines of HTML in `server.py` for the landing page | Unreviewable, conflates code and content. | HTML lives in `templates/`, loaded via `importlib.resources`. Hostname injected client-side from `window.location.origin`. |
| Documenting state in CLAUDE.md (env-var tables, tool tables) | Drift between `README.md` and `CLAUDE.md`. | Functional state lives in `README.md` / `PRIVACY.md` — this file *links*. |
| Inventing instead of reading the SDK | "Pas d'invention" — user-level rule that overrides everything. | One Context7 query is cheaper than one rejected PR. |

## When in doubt

Re-read the user-level instructions in `~/.claude/CLAUDE.md` (tone, doc-first
rule, no `git add -A`, no `--no-verify`, etc.) — they always override the
defaults baked into the model.
