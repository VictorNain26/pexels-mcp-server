"""Pydantic enums and input schemas for Pexels MCP tools.

Each model uses ``ConfigDict(extra="forbid", str_strip_whitespace=True)`` so that
unknown fields are rejected and string inputs are trimmed. The tool functions in
``server.py`` instantiate these models internally to validate the call arguments
before hitting the Pexels API.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .constants import DEFAULT_PAGE, DEFAULT_PER_PAGE, MAX_PER_PAGE


class ResponseFormat(str, Enum):
    """Output format the tool returns to the agent."""

    MARKDOWN = "markdown"
    JSON = "json"


class Orientation(str, Enum):
    """Image / video orientation supported by the Pexels API."""

    LANDSCAPE = "landscape"
    PORTRAIT = "portrait"
    SQUARE = "square"


class PhotoSize(str, Enum):
    """Minimum photo size buckets exposed by the Pexels search endpoint."""

    LARGE = "large"
    MEDIUM = "medium"
    SMALL = "small"


class VideoSize(str, Enum):
    """Minimum video resolution buckets exposed by the Pexels search endpoint."""

    LARGE = "large"
    MEDIUM = "medium"
    SMALL = "small"


class PhotoColor(str, Enum):
    """Named colors accepted by the Pexels photo search endpoint."""

    RED = "red"
    ORANGE = "orange"
    YELLOW = "yellow"
    GREEN = "green"
    TURQUOISE = "turquoise"
    BLUE = "blue"
    VIOLET = "violet"
    PINK = "pink"
    BROWN = "brown"
    BLACK = "black"
    GRAY = "gray"
    WHITE = "white"


class CollectionMediaType(str, Enum):
    """Filter for the ``type`` query parameter on the collection endpoint."""

    PHOTOS = "photos"
    VIDEOS = "videos"


class SortOrder(str, Enum):
    """Sort direction for collection contents."""

    ASC = "asc"
    DESC = "desc"


# 28 locales accepted by Pexels. Documented at
# https://www.pexels.com/api/documentation/#photos-search
SUPPORTED_LOCALES: tuple[str, ...] = (
    "en-US",
    "pt-BR",
    "es-ES",
    "ca-ES",
    "de-DE",
    "it-IT",
    "fr-FR",
    "sv-SE",
    "id-ID",
    "pl-PL",
    "ja-JP",
    "zh-TW",
    "zh-CN",
    "ko-KR",
    "th-TH",
    "nl-NL",
    "hu-HU",
    "vi-VN",
    "cs-CZ",
    "da-DK",
    "fi-FI",
    "uk-UA",
    "el-GR",
    "ro-RO",
    "nb-NO",
    "sk-SK",
    "tr-TR",
    "ru-RU",
)


class _StrictModel(BaseModel):
    """Base for every input model: forbid unknown fields, strip strings."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Pagination(_StrictModel):
    """Shared pagination knobs."""

    page: int = Field(
        default=DEFAULT_PAGE,
        ge=1,
        description="Page number, starting at 1.",
    )
    per_page: int = Field(
        default=DEFAULT_PER_PAGE,
        ge=1,
        le=MAX_PER_PAGE,
        description=f"Items per page. Min 1, max {MAX_PER_PAGE}.",
    )


class SearchPhotosParams(Pagination):
    """Inputs for ``pexels_search_photos``."""

    query: str = Field(min_length=1, max_length=200, description="Search query string.")
    orientation: Orientation | None = Field(default=None, description="Photo orientation filter.")
    size: PhotoSize | None = Field(default=None, description="Minimum photo size bucket.")
    color: str | None = Field(
        default=None,
        max_length=32,
        description="Color filter. One of the named colors or a 6-digit hex without leading '#'.",
    )
    locale: str | None = Field(
        default=None,
        max_length=16,
        description="BCP-47 locale (e.g. en-US, fr-FR). See Pexels docs for the full list.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format. markdown for humans, json for downstream processing.",
    )


class CuratedPhotosParams(Pagination):
    """Inputs for ``pexels_curated_photos``."""

    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class GetPhotoParams(_StrictModel):
    """Inputs for ``pexels_get_photo``."""

    photo_id: int = Field(ge=1, description="Pexels photo id.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class SearchVideosParams(Pagination):
    """Inputs for ``pexels_search_videos``."""

    query: str = Field(min_length=1, max_length=200, description="Search query string.")
    orientation: Orientation | None = Field(default=None, description="Video orientation filter.")
    size: VideoSize | None = Field(default=None, description="Minimum video resolution bucket.")
    locale: str | None = Field(
        default=None,
        max_length=16,
        description="BCP-47 locale (e.g. en-US, fr-FR).",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class PopularVideosParams(Pagination):
    """Inputs for ``pexels_popular_videos``."""

    min_width: int | None = Field(default=None, ge=1, description="Minimum video width in pixels.")
    min_height: int | None = Field(
        default=None,
        ge=1,
        description="Minimum video height in pixels.",
    )
    min_duration: int | None = Field(
        default=None,
        ge=1,
        description="Minimum video duration in seconds.",
    )
    max_duration: int | None = Field(
        default=None,
        ge=1,
        description="Maximum video duration in seconds.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class GetVideoParams(_StrictModel):
    """Inputs for ``pexels_get_video``."""

    video_id: int = Field(ge=1, description="Pexels video id.")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class FeaturedCollectionsParams(Pagination):
    """Inputs for ``pexels_list_featured_collections``."""

    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class CollectionMediaParams(Pagination):
    """Inputs for ``pexels_get_collection_media``."""

    collection_id: str = Field(min_length=1, max_length=64, description="Pexels collection id.")
    type: CollectionMediaType | None = Field(
        default=None,
        description="Filter the collection to photos or videos only. Defaults to both.",
    )
    sort: SortOrder | None = Field(
        default=None,
        description="Sort by creation date (asc or desc).",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)
