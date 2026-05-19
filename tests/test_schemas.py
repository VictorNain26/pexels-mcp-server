"""Validation tests for the Pydantic input schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pexels_mcp_server.schemas import (
    CollectionMediaParams,
    GetPhotoParams,
    Orientation,
    PopularVideosParams,
    ResponseFormat,
    SearchPhotosParams,
)


def test_search_photos_defaults() -> None:
    params = SearchPhotosParams(query="dogs")
    assert params.query == "dogs"
    assert params.page == 1
    assert params.per_page == 15
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


def test_popular_videos_rejects_negative_duration() -> None:
    with pytest.raises(ValidationError):
        PopularVideosParams(min_duration=-1)


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
