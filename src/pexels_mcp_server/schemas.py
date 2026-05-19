"""Pydantic input schemas for the Pexels MCP tools.

Every model uses ``ConfigDict(extra="forbid", str_strip_whitespace=True)``
so unknown fields are rejected and string inputs are trimmed. A
model_validator coerces explicit ``null`` on any field with a non-None
default — claude.ai serializes optional fields as ``null`` instead of
omitting them, and strict pydantic would otherwise reject the call.

Descriptions are kept short on purpose: each char ends up in the tool's
JSON Schema sent to the LLM at conversation init. Common filters share
factory helpers so the description string lives once in source.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticUndefined

from .constants import DEFAULT_PAGE, DEFAULT_PER_PAGE, MAX_PER_PAGE

_HEX_COLOR_RE = re.compile(r"^[0-9A-Fa-f]{6}$")
_COLLECTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_DIMENSION_PX = 100_000
_ASPECT_RATIO_HELP = (
    "aspect_ratio must look like 'W:H' (e.g. '16:9', '1:1', '9:16') "
    "or a positive decimal with an explicit dot (e.g. '1.5')."
)


def parse_aspect_ratio(value: str) -> float:
    """Parse '16:9', '1:1', '0.5625' into a positive float ratio.

    Rejects bare integers like ``"16"`` — too likely a half-typed
    ``"16:9"`` for safety.
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


class Orientation(str, Enum):
    LANDSCAPE = "landscape"
    PORTRAIT = "portrait"
    SQUARE = "square"


class PhotoSize(str, Enum):
    LARGE = "large"
    MEDIUM = "medium"
    SMALL = "small"


class VideoSize(str, Enum):
    LARGE = "large"
    MEDIUM = "medium"
    SMALL = "small"


class PhotoColor(str, Enum):
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
    PHOTOS = "photos"
    VIDEOS = "videos"


class SortOrder(str, Enum):
    ASC = "asc"
    DESC = "desc"


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
    """Forbid unknown fields, strip strings, coerce null to default."""

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
    if value is None:
        return None
    if value not in SUPPORTED_LOCALES:
        raise ValueError(f"locale must be one of {', '.join(SUPPORTED_LOCALES)}; got {value!r}.")
    return value


def _validate_aspect_ratio(value: str | None) -> str | None:
    if value is None:
        return None
    parse_aspect_ratio(value)
    return value


# Common field factories (one source of truth for descriptions).


def _min_width_field() -> Any:
    return Field(
        default=None,
        ge=1,
        le=_MAX_DIMENSION_PX,
        description="Minimum native width in pixels (post-hoc filter).",
    )


def _min_height_field() -> Any:
    return Field(
        default=None,
        ge=1,
        le=_MAX_DIMENSION_PX,
        description="Minimum native height in pixels (post-hoc filter).",
    )


def _aspect_ratio_field() -> Any:
    return Field(
        default=None,
        max_length=20,
        description=(
            "Target aspect ratio, e.g. '16:9' hero, '1:1' Instagram, '9:16' Story, "
            "'4:5' LinkedIn. ±5% tolerance (post-hoc filter)."
        ),
    )


class Pagination(_StrictModel):
    page: int = Field(default=DEFAULT_PAGE, ge=1, le=10_000)
    per_page: int = Field(
        default=DEFAULT_PER_PAGE,
        ge=1,
        le=MAX_PER_PAGE,
        description=f"Items per page (1..{MAX_PER_PAGE}).",
    )


class SearchPhotosParams(Pagination):
    query: str = Field(min_length=1, max_length=200, description="Search query.")
    orientation: Orientation | None = Field(default=None)
    size: PhotoSize | None = Field(default=None)
    color: str | None = Field(
        default=None,
        max_length=32,
        description="Named color or 6-digit hex without '#'.",
    )
    locale: str | None = Field(default=None, max_length=16)
    min_width: int | None = _min_width_field()
    min_height: int | None = _min_height_field()
    aspect_ratio: str | None = _aspect_ratio_field()

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
        raise ValueError("color must be one of " + ", ".join(sorted(named)) + " or a 6-digit hex.")

    @field_validator("locale")
    @classmethod
    def _check_locale(cls, value: str | None) -> str | None:
        return _validate_locale(value)

    @field_validator("aspect_ratio")
    @classmethod
    def _check_aspect_ratio(cls, value: str | None) -> str | None:
        return _validate_aspect_ratio(value)


class GetPhotoParams(_StrictModel):
    photo_id: int = Field(ge=1)


class SearchVideosParams(Pagination):
    query: str = Field(min_length=1, max_length=200)
    orientation: Orientation | None = Field(default=None)
    size: VideoSize | None = Field(default=None)
    locale: str | None = Field(default=None, max_length=16)
    min_width: int | None = _min_width_field()
    min_height: int | None = _min_height_field()
    aspect_ratio: str | None = _aspect_ratio_field()

    @field_validator("locale")
    @classmethod
    def _check_locale(cls, value: str | None) -> str | None:
        return _validate_locale(value)

    @field_validator("aspect_ratio")
    @classmethod
    def _check_aspect_ratio(cls, value: str | None) -> str | None:
        return _validate_aspect_ratio(value)


class GetVideoParams(_StrictModel):
    video_id: int = Field(ge=1)


class CollectionMediaParams(Pagination):
    collection_id: str = Field(min_length=1, max_length=64)
    type: CollectionMediaType | None = Field(default=None)
    sort: SortOrder | None = Field(default=None)
    min_width: int | None = _min_width_field()
    min_height: int | None = _min_height_field()
    aspect_ratio: str | None = _aspect_ratio_field()

    @field_validator("aspect_ratio")
    @classmethod
    def _check_aspect_ratio(cls, value: str | None) -> str | None:
        return _validate_aspect_ratio(value)

    @field_validator("collection_id")
    @classmethod
    def _check_collection_id(cls, value: str) -> str:
        if not _COLLECTION_ID_RE.match(value):
            raise ValueError("collection_id must contain only letters, digits, '-' and '_'.")
        return value
