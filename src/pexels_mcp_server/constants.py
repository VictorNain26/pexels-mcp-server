"""Module-wide constants for the Pexels MCP server."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Final

BASE_URL: Final[str] = "https://api.pexels.com"
PHOTOS_PREFIX: Final[str] = "/v1"
# Pexels deprecated the legacy /videos/* root in favor of /v1/videos/* per
# https://www.pexels.com/api/documentation/ (Videos section).
VIDEOS_PREFIX: Final[str] = "/v1/videos"
COLLECTIONS_PREFIX: Final[str] = "/v1/collections"

# Hosts the preview tool will fetch from. Anything else is rejected upfront
# to keep the tool from doubling as an SSRF gadget.
PEXELS_CDN_HOSTS: Final[frozenset[str]] = frozenset({"images.pexels.com"})

# Maximum thumbnails returned per pexels_preview_media call. Each one is
# encoded as base64 in the MCP response so the budget matters.
PREVIEW_MAX_COUNT: Final[int] = 6

# Per-request timeout and total cap for the preview fetcher.
PREVIEW_FETCH_TIMEOUT_SECONDS: Final[float] = 10.0
PREVIEW_MAX_BYTES: Final[int] = 256 * 1024  # 256 KB per thumbnail; tiny is ~30 KB

DEFAULT_PER_PAGE: Final[int] = 15
MAX_PER_PAGE: Final[int] = 80
DEFAULT_PAGE: Final[int] = 1

HTTP_TIMEOUT_SECONDS: Final[float] = 15.0
RETRY_BACKOFF_SECONDS: Final[float] = 1.0


def _resolve_version() -> str:
    try:
        return version("pexels-mcp")
    except PackageNotFoundError:
        return "0.0.0+unknown"


USER_AGENT: Final[str] = (
    f"pexels-mcp-server/{_resolve_version()} (+https://github.com/VictorNain26/pexels-mcp-server)"
)

PEXELS_ATTRIBUTION: Final[str] = "Photos provided by Pexels (https://www.pexels.com)"
