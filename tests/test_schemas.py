"""Validation tests for the Pydantic input schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pexels_mcp_server.schemas import (
    CollectionMediaParams,
    GetPhotoParams,
    Orientation,
    ResponseFormat,
    SearchPhotosParams,
)


def test_search_photos_defaults() -> None:
    params = SearchPhotosParams(query="dogs")
    assert params.query == "dogs"
    assert params.page == 1
    assert params.per_page == 5
    assert params.response_format == ResponseFormat.JSON
    assert params.orientation is None


def test_search_photos_strips_whitespace() -> None:
    params = SearchPhotosParams(query="  cats  ")
    assert params.query == "cats"


def test_search_photos_rejects_per_page_above_max() -> None:
    with pytest.raises(ValidationError):
        SearchPhotosParams(query="dogs", per_page=200)


def test_search_photos_rejects_empty_query() -> None:
    with pytest.raises(ValidationError):
        SearchPhotosParams(query="")


def test_search_photos_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        SearchPhotosParams(query="dogs", foo="bar")  # type: ignore[call-arg]


def test_search_photos_rejects_invalid_orientation() -> None:
    with pytest.raises(ValidationError):
        SearchPhotosParams(query="dogs", orientation="wide")  # type: ignore[arg-type]


def test_search_photos_accepts_enum_value() -> None:
    params = SearchPhotosParams(query="dogs", orientation=Orientation.LANDSCAPE)
    assert params.orientation is Orientation.LANDSCAPE


def test_get_photo_requires_positive_id() -> None:
    with pytest.raises(ValidationError):
        GetPhotoParams(photo_id=0)


def test_collection_media_strips_whitespace() -> None:
    params = CollectionMediaParams(collection_id="  abc123  ")
    assert params.collection_id == "abc123"


def test_response_format_enum_round_trip() -> None:
    params = SearchPhotosParams(query="dogs", response_format=ResponseFormat.JSON)
    assert params.response_format.value == "json"


def test_search_photos_accepts_named_color() -> None:
    params = SearchPhotosParams(query="dogs", color="RED")
    assert params.color == "red"


def test_search_photos_accepts_hex_color() -> None:
    params = SearchPhotosParams(query="dogs", color="A1B2C3")
    assert params.color == "a1b2c3"


def test_search_photos_rejects_unknown_color() -> None:
    with pytest.raises(ValidationError):
        SearchPhotosParams(query="dogs", color="banana")


def test_search_photos_rejects_malformed_hex() -> None:
    with pytest.raises(ValidationError):
        SearchPhotosParams(query="dogs", color="#ff00ff")


def test_search_photos_rejects_unknown_locale() -> None:
    with pytest.raises(ValidationError) as excinfo:
        SearchPhotosParams(query="dogs", locale="xx-XX")
    assert "locale must be one of" in str(excinfo.value)


def test_search_photos_accepts_supported_locale() -> None:
    params = SearchPhotosParams(query="dogs", locale="fr-FR")
    assert params.locale == "fr-FR"


def test_collection_media_rejects_path_traversal() -> None:
    with pytest.raises(ValidationError):
        CollectionMediaParams(collection_id="../photos")


def test_collection_media_rejects_slash() -> None:
    with pytest.raises(ValidationError):
        CollectionMediaParams(collection_id="abc/def")


def test_collection_media_accepts_alphanumeric_with_dashes() -> None:
    params = CollectionMediaParams(collection_id="abc-123_def")
    assert params.collection_id == "abc-123_def"


# --- null coercion (defensive against MCP clients that serialize defaults
# as `null` instead of omitting the key) ----------------------------------


def test_response_format_null_is_coerced_to_default() -> None:
    """claude.ai web ships ``response_format: null`` in the wild on every
    tool call. The strict enum used to reject; now it normalizes to the
    field default (``json``) so the call succeeds."""
    params = SearchPhotosParams(query="x", response_format=None)  # type: ignore[arg-type]
    assert params.response_format == ResponseFormat.JSON


def test_page_null_is_coerced_to_default() -> None:
    """Same defensive pattern on ``page`` — defaulting integer fields fail
    strict validation on null too."""
    params = SearchPhotosParams(query="x", page=None, per_page=None)  # type: ignore[arg-type]
    assert params.page == 1
    assert params.per_page == 5


def test_orientation_null_remains_none() -> None:
    """``orientation: Orientation | None = None`` keeps its semantics: null
    means 'no orientation filter', not 'use a default orientation'."""
    params = SearchPhotosParams(query="x", orientation=None)
    assert params.orientation is None


def test_required_field_null_still_fails() -> None:
    """The null-coercion must not paper over missing required fields. A
    ``query=null`` on SearchPhotosParams is a real error."""
    with pytest.raises(ValidationError):
        SearchPhotosParams(query=None)  # type: ignore[arg-type]


# --- marketing filters: aspect_ratio + min_width + min_height ------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("16:9", 16 / 9),
        ("1:1", 1.0),
        ("9:16", 9 / 16),
        ("4:5", 0.8),
        ("21:9", 21 / 9),
        ("1.5", 1.5),
        ("0.5625", 0.5625),
        (" 16 : 9 ", 16 / 9),  # whitespace tolerated
    ],
)
def test_parse_aspect_ratio_accepts_valid_inputs(value: str, expected: float) -> None:
    from pexels_mcp_server.schemas import parse_aspect_ratio

    assert parse_aspect_ratio(value) == pytest.approx(expected)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "16",
        "16:0",
        "0:9",
        "-16:9",
        "16x9",
        "sixteen:nine",
        "16:9:9",
        ":",
    ],
)
def test_parse_aspect_ratio_rejects_invalid_inputs(value: str) -> None:
    from pexels_mcp_server.schemas import parse_aspect_ratio

    with pytest.raises(ValueError, match="aspect_ratio"):
        parse_aspect_ratio(value)


def test_search_photos_accepts_aspect_ratio_and_min_dims() -> None:
    params = SearchPhotosParams(
        query="cat",
        aspect_ratio="16:9",
        min_width=1920,
        min_height=1080,
    )
    assert params.aspect_ratio == "16:9"
    assert params.min_width == 1920
    assert params.min_height == 1080


def test_search_photos_rejects_invalid_aspect_ratio() -> None:
    with pytest.raises(ValidationError):
        SearchPhotosParams(query="cat", aspect_ratio="not-a-ratio")


def test_search_photos_rejects_negative_min_width() -> None:
    with pytest.raises(ValidationError):
        SearchPhotosParams(query="cat", min_width=0)


def test_search_photos_rejects_oversized_min_width() -> None:
    """The 100 000 px cap stops a typo from yielding an unreachable filter."""
    with pytest.raises(ValidationError):
        SearchPhotosParams(query="cat", min_width=200_000)


def test_aspect_ratio_tolerance_no_longer_exposed_as_param() -> None:
    """The 5% tolerance is now hardcoded — exposing it as a param added
    noise to the tool input schema (more tokens at conversation init).
    A future user-facing knob can come back if real demand shows up."""
    with pytest.raises(ValidationError):
        SearchPhotosParams(query="cat", aspect_ratio_tolerance=0.1)  # type: ignore[call-arg]
