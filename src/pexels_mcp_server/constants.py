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
