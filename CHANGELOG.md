# Changelog

All notable changes to this project are documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added
- `pexels_preview_media` tool that fetches Pexels CDN thumbnails and returns them as MCP `ImageContent` so vision-capable agents can pick the best image visually after a search.
- GitHub repo metadata: `dependabot.yml` (weekly grouped updates), `SECURITY.md` (private vulnerability reporting), `CONTRIBUTING.md`, PR template and issue templates.
- CI: concurrency group so superseded runs cancel themselves.
- `py.typed` marker so downstream consumers respect the inline type annotations.
- `.editorconfig` for editor-agnostic indentation rules.

## [0.2.0] - 2026-05-19

### Changed
- **Breaking**: every tool now defaults to `response_format="json"` (was `markdown`). Markdown stays available as an opt-in for human inspection.
- **Breaking**: photo projection trimmed. Dropped `liked`, `photographer_id`, `avg_color`, and 4 of the 6 per-orientation `src` URLs. Renamed `url` → `page_url`, exposed `image_url` (original) and `thumbnail_url` (medium) at the top level.
- **Breaking**: video projection trimmed. Dropped `video_pictures`, `tags`, `avg_color`, `full_res`. Kept only the top 3 files by resolution and added `total_files_available` so the agent knows there is more.
- Migrated video endpoints from the deprecated `/videos/*` to `/v1/videos/*` per the latest Pexels API documentation.
- Rewrote every tool docstring to follow Anthropic's [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) guidance: concrete USE WHEN / DO NOT USE WHEN examples, output-shape teaser inline.
- README repositioned as an MCP-for-agents primer. Install via `uvx --from git+...`; no PyPI dependency for first-class use.

### Fixed
- `SearchPhotosParams.color` now actually validates against the `PhotoColor` enum or a 6-digit hex; previously the enum existed but was bypassed.
- `format_collection_media` no longer renders `None` when a payload omits `id`; uses `_safe` like the rest of the formatters.
- `USER_AGENT` now reads the version from `importlib.metadata` so it stays in sync with `pyproject.toml`.

### Removed
- Redundant `PEXELS_API_KEY` check in the FastMCP lifespan. `PexelsClient.__init__` is the source of truth; the CLI entrypoint exits cleanly before the lifespan ever runs.

## [0.1.0] - 2026-05-18

### Added
- Initial release. Eight read-only tools wrapping the Pexels REST API: `pexels_search_photos`, `pexels_curated_photos`, `pexels_get_photo`, `pexels_search_videos`, `pexels_popular_videos`, `pexels_get_video`, `pexels_list_featured_collections`, `pexels_get_collection_media`.
- Async `httpx` client with retry on 5xx and `X-Ratelimit-*` parsing.
- Pydantic v2 strict input models (`extra="forbid"`).
- Stdio and Streamable HTTP transports.
- CI matrix on Python 3.10, 3.11, 3.12.

[Unreleased]: https://github.com/VictorNain26/pexels-mcp-server/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/VictorNain26/pexels-mcp-server/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/VictorNain26/pexels-mcp-server/releases/tag/v0.1.0
