"""Pydantic enums and input schemas for Pexels MCP tools.

Each model uses ``ConfigDict(extra="forbid", str_strip_whitespace=True)`` so that
unknown fields are rejected and string inputs are trimmed. The tool functions in
``server.py`` instantiate these models internally to validate the call arguments
before hitting the Pexels API.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticUndefined

from .constants import (
    DEFAULT_PAGE,
    DEFAULT_PER_PAGE,
    MAX_PER_PAGE,
)

_HEX_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
# Pexels collection IDs are short alphanumeric strings (with optional dashes
# and underscores). Anchoring the regex prevents path-injection patterns like
# "../photos" landing in URL paths.
_COLLECTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# A 100 000 px upper bound on min_width/min_height stops a typo from
# becoming an unreachable filter (Pexels' largest assets cap around 25 000 px).
_MAX_DIMENSION_PX = 100_000

_ASPECT_RATIO_HELP = (
    "aspect_ratio must look like 'W:H' (e.g. '16:9', '1:1', '9:16') "
    "or a positive decimal with an explicit dot (e.g. '1.5', '0.5625')."
)


def parse_aspect_ratio(value: str) -> float:
    """Parse '16:9', '1:1', '0.5625' (etc.) into a positive float ratio.

    A bare integer like ``"16"`` is rejected even though Python could
    parse it as a float — it is much more likely a half-typed ``"16:9"``
    than an intentional 16:1 ratio. Requiring the explicit dot avoids the
    foot-gun.

    Raises :class:`ValueError` on any malformed input so a Pydantic
    field_validator can surface an actionable message to the agent.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(_ASPECT_RATIO_HELP)
    if ":" in cleaned:
        parts = cleaned.split(":")
        if len(parts) != 2:
            raise ValueError(_ASPECT_RATIO_HELP)
        try:
            width = float(parts[0].strip())
            height = float(parts[1].strip())
        except ValueError as exc:
            raise ValueError(_ASPECT_RATIO_HELP) from exc
        if width <= 0 or height <= 0:
            raise ValueError("aspect_ratio components must both be positive.")
        return width / height
    if "." not in cleaned:
        raise ValueError(_ASPECT_RATIO_HELP)
    try:
        ratio = float(cleaned)
    except ValueError as exc:
        raise ValueError(_ASPECT_RATIO_HELP) from exc
    if ratio <= 0:
        raise ValueError("aspect_ratio must be positive.")
    return ratio


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
    """Base for every input model: forbid unknown fields, strip strings,
    and coerce explicit ``null`` on a defaulted field to the field's default.

    The null-coercion is there because some MCP clients (claude.ai web is
    one) serialize every schema field on every tool call, including
    optional ones with a default — they send ``"response_format": null``
    instead of omitting the key. Strict pydantic validation rejects the
    null and the tool call fails with a confusing "Input should be
    'markdown' or 'json'" error.

    With this validator, ``{"response_format": null}`` is normalized to
    ``{"response_format": "json"}`` (the field's default) before the type
    check runs, so the call succeeds. Required fields (no default) and
    truly-nullable fields (``Foo | None = None``) are untouched: the
    former still fail validation on null, the latter still accept null.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _coerce_nulls_to_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for name, info in cls.model_fields.items():
            if name not in data or data[name] is not None:
                continue
            if info.default is not PydanticUndefined:
                data[name] = info.default
            elif info.default_factory is not None:
                data[name] = info.default_factory()  # type: ignore[call-arg]
        return data


def _validate_locale(value: str | None) -> str | None:
    """Reject locales not present in the documented Pexels allowlist.

    The Pexels search endpoint silently ignores unknown locales rather than
    erroring, which means a malformed value just degrades relevance without
    feedback. Failing fast on an unknown locale keeps the agent honest.
    """
    if value is None:
        return None
    if value not in SUPPORTED_LOCALES:
        raise ValueError(f"locale must be one of {', '.join(SUPPORTED_LOCALES)}; got {value!r}.")
    return value


class Pagination(_StrictModel):
    """Shared pagination knobs."""

    page: int = Field(
        default=DEFAULT_PAGE,
        ge=1,
        # Pexels caps at ~roughly this anyway; the upper bound stops a caller
        # from wasting an outbound HTTP round-trip on a 999_999_999 page.
        le=10_000,
        description="Page number, starting at 1.",
    )
    per_page: int = Field(
        default=DEFAULT_PER_PAGE,
        ge=1,
        le=MAX_PER_PAGE,
        description=f"Items per page. Min 1, max {MAX_PER_PAGE}.",
    )


# Standalone field definition reused by every photo/video schema. Carries
# the per-call switch that toggles inline ``ImageContent`` previews.
#
# Default is False as of 2026-05-19: embedding 15 base64 thumbnails per
# call burns ~1300 vision tokens x N tool calls and quickly fills the
# claude.ai conversation context (users report "Conversation too long"
# errors after 3-4 search calls in one chat). The Markdown image syntax
# the tool docstring instructs the LLM to use (``![alt](image_url)``)
# delivers the same inline-display UX without the tokens — claude.ai
# renders external images natively from Markdown.
#
# Opt in (``include_previews=true``) when the agent really needs to do
# a vision-based pick on top of Pexels' relevance ranking.
def _include_previews_field() -> Any:
    return Field(
        default=False,
        description=(
            "When true, the server fetches the medium thumbnail for each "
            "result from images.pexels.com and embeds it as an MCP "
            "ImageContent block so the model can vision-pick on top of "
            "Pexels' relevance ranking. Default false: 15 base64 "
            "thumbnails per call (~1300 vision tokens) fills the chat "
            "context fast and is not needed for the user-visible inline "
            "display — that is delivered by the LLM rendering "
            "`![alt](image_url)` Markdown in its response, which "
            "claude.ai renders natively without any tokens spent on "
            "embedded previews."
        ),
    )


def _min_width_field() -> Any:
    return Field(
        default=None,
        ge=1,
        le=_MAX_DIMENSION_PX,
        description=(
            "Server-side post-filter: drop any result whose native width is "
            "below this pixel value. Useful for print (need ~4000 px for "
            "A4 at 300 DPI) or hero banners (need ~1920 px minimum)."
        ),
    )


def _min_height_field() -> Any:
    return Field(
        default=None,
        ge=1,
        le=_MAX_DIMENSION_PX,
        description=(
            "Server-side post-filter: drop any result whose native height is "
            "below this pixel value."
        ),
    )


def _aspect_ratio_field() -> Any:
    return Field(
        default=None,
        max_length=20,
        description=(
            "Server-side post-filter on width/height ratio. Accepts 'W:H' "
            "(e.g. '16:9' for hero, '1:1' for Instagram square, '9:16' for "
            "Story, '4:5' for LinkedIn portrait, '21:9' for ultrawide) or a "
            "positive decimal ('1.5', '0.5625'). Matched within "
            "``aspect_ratio_tolerance`` of the target."
        ),
    )


def _aspect_ratio_tolerance_field() -> Any:
    return Field(
        default=0.05,
        ge=0.0,
        le=0.5,
        description=(
            "Relative tolerance applied to ``aspect_ratio`` matching, "
            "default 5%. With ratio=1.0 and tolerance=0.05, anything from "
            "0.95 to 1.05 matches. Bump to 0.10 for more candidates, drop "
            "to 0.02 for stricter matching."
        ),
    )


class SearchPhotosParams(Pagination):
    """Inputs for ``pexels_search_photos``."""

    query: str = Field(min_length=1, max_length=200, description="Search query string.")
    orientation: Orientation | None = Field(default=None, description="Photo orientation filter.")
    size: PhotoSize | None = Field(default=None, description="Minimum photo size bucket.")
    color: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "Color filter. One of: "
            + ", ".join(c.value for c in PhotoColor)
            + " — or a 6-digit hex without leading '#'."
        ),
    )
    locale: str | None = Field(
        default=None,
        max_length=16,
        description="BCP-47 locale (e.g. en-US, fr-FR). See Pexels docs for the full list.",
    )
    min_width: int | None = _min_width_field()
    min_height: int | None = _min_height_field()
    aspect_ratio: str | None = _aspect_ratio_field()
    aspect_ratio_tolerance: float = _aspect_ratio_tolerance_field()
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON,
        description=(
            "Output shape. 'json' returns a structured envelope agents can "
            "parse directly. 'markdown' returns a human-readable bullet list "
            "for inspection."
        ),
    )
    include_previews: bool = _include_previews_field()

    @field_validator("aspect_ratio")
    @classmethod
    def _check_aspect_ratio(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_aspect_ratio(value)
        return value

    @field_validator("color")
    @classmethod
    def _check_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        named = {c.value for c in PhotoColor}
        if value.lower() in named:
            return value.lower()
        if _HEX_COLOR_RE.match(value):
            return value.lower()
        raise ValueError(
            "color must be one of " + ", ".join(sorted(named)) + " or a 6-digit hex without '#'."
        )

    @field_validator("locale")
    @classmethod
    def _check_locale(cls, value: str | None) -> str | None:
        return _validate_locale(value)


class CuratedPhotosParams(Pagination):
    """Inputs for ``pexels_curated_photos``."""

    min_width: int | None = _min_width_field()
    min_height: int | None = _min_height_field()
    aspect_ratio: str | None = _aspect_ratio_field()
    aspect_ratio_tolerance: float = _aspect_ratio_tolerance_field()
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)
    include_previews: bool = _include_previews_field()

    @field_validator("aspect_ratio")
    @classmethod
    def _check_aspect_ratio(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_aspect_ratio(value)
        return value


class GetPhotoParams(_StrictModel):
    """Inputs for ``pexels_get_photo``."""

    photo_id: int = Field(ge=1, description="Pexels photo id.")
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)
    include_previews: bool = _include_previews_field()


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
    min_width: int | None = _min_width_field()
    min_height: int | None = _min_height_field()
    aspect_ratio: str | None = _aspect_ratio_field()
    aspect_ratio_tolerance: float = _aspect_ratio_tolerance_field()
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)
    include_previews: bool = _include_previews_field()

    @field_validator("locale")
    @classmethod
    def _check_locale(cls, value: str | None) -> str | None:
        return _validate_locale(value)

    @field_validator("aspect_ratio")
    @classmethod
    def _check_aspect_ratio(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_aspect_ratio(value)
        return value


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
    aspect_ratio: str | None = _aspect_ratio_field()
    aspect_ratio_tolerance: float = _aspect_ratio_tolerance_field()
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)
    include_previews: bool = _include_previews_field()

    @field_validator("aspect_ratio")
    @classmethod
    def _check_aspect_ratio(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_aspect_ratio(value)
        return value


class GetVideoParams(_StrictModel):
    """Inputs for ``pexels_get_video``."""

    video_id: int = Field(ge=1, description="Pexels video id.")
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)
    include_previews: bool = _include_previews_field()


class FeaturedCollectionsParams(Pagination):
    """Inputs for ``pexels_list_featured_collections``."""

    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


class MyCollectionsParams(Pagination):
    """Inputs for ``pexels_get_my_collections``."""

    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)


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
    min_width: int | None = _min_width_field()
    min_height: int | None = _min_height_field()
    aspect_ratio: str | None = _aspect_ratio_field()
    aspect_ratio_tolerance: float = _aspect_ratio_tolerance_field()
    response_format: ResponseFormat = Field(default=ResponseFormat.JSON)
    include_previews: bool = _include_previews_field()

    @field_validator("aspect_ratio")
    @classmethod
    def _check_aspect_ratio(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_aspect_ratio(value)
        return value

    @field_validator("collection_id")
    @classmethod
    def _check_collection_id(cls, value: str) -> str:
        if not _COLLECTION_ID_RE.match(value):
            raise ValueError("collection_id must contain only letters, digits, '-' and '_'.")
        return value
