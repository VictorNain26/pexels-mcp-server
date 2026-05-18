"""Typed dicts mirroring the Pexels API JSON responses.

These types are documentation of the shape of upstream payloads. Mandatory
fields are kept; everything optional uses ``NotRequired`` so partial payloads do
not break type checking.
"""

from __future__ import annotations

from typing import TypedDict

from typing_extensions import NotRequired


class PhotoSrcDict(TypedDict, total=False):
    """``src`` block on a Pexels photo."""

    original: str
    large2x: str
    large: str
    medium: str
    small: str
    portrait: str
    landscape: str
    tiny: str


class PhotoDict(TypedDict):
    """Single photo as returned by Pexels."""

    id: int
    width: int
    height: int
    url: str
    photographer: str
    photographer_url: str
    photographer_id: int
    avg_color: str
    src: PhotoSrcDict
    liked: bool
    alt: str


class VideoFileDict(TypedDict, total=False):
    """An entry of the ``video_files`` array."""

    id: int
    quality: str
    file_type: str
    width: int
    height: int
    fps: float
    link: str


class VideoPictureDict(TypedDict, total=False):
    """Preview picture for a video."""

    id: int
    nr: int
    picture: str


class VideoUserDict(TypedDict, total=False):
    """User block embedded in a video object."""

    id: int
    name: str
    url: str


class VideoDict(TypedDict):
    """Single video as returned by Pexels."""

    id: int
    width: int
    height: int
    duration: int
    full_res: NotRequired[str | None]
    tags: NotRequired[list[str]]
    url: str
    image: str
    avg_color: NotRequired[str | None]
    user: VideoUserDict
    video_files: list[VideoFileDict]
    video_pictures: list[VideoPictureDict]


class CollectionDict(TypedDict, total=False):
    """A collection summary."""

    id: str
    title: str
    description: str | None
    private: bool
    media_count: int
    photos_count: int
    videos_count: int


class RateLimitDict(TypedDict, total=False):
    """Parsed ``X-Ratelimit-*`` headers."""

    limit: int
    remaining: int
    reset: str
