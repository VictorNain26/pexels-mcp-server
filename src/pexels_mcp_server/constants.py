"""Module-wide constants for the Pexels MCP server."""

from __future__ import annotations

from typing import Final

BASE_URL: Final[str] = "https://api.pexels.com"
PHOTOS_PREFIX: Final[str] = "/v1"
VIDEOS_PREFIX: Final[str] = "/videos"
COLLECTIONS_PREFIX: Final[str] = "/v1/collections"

DEFAULT_PER_PAGE: Final[int] = 15
MAX_PER_PAGE: Final[int] = 80
DEFAULT_PAGE: Final[int] = 1

HTTP_TIMEOUT_SECONDS: Final[float] = 15.0
RETRY_BACKOFF_SECONDS: Final[float] = 1.0

USER_AGENT: Final[str] = (
    "pexels-mcp-server/0.1.0 (+https://github.com/VictorNain26/pexels-mcp-server)"
)

PEXELS_ATTRIBUTION: Final[str] = "Photos provided by Pexels (https://www.pexels.com)"
