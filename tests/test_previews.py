"""Tests for the URL whitelist and the thumbnail fetcher."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pytest_httpx import HTTPXMock

from pexels_mcp_server.previews import fetch_thumbnails
from pexels_mcp_server.schemas import PreviewMediaParams

_OK_URL = "https://images.pexels.com/photos/1/sample.jpeg"
_OK_URL_2 = "https://images.pexels.com/photos/2/sample.jpeg"


def test_preview_params_rejects_non_pexels_host() -> None:
    with pytest.raises(ValidationError) as excinfo:
        PreviewMediaParams(thumbnail_urls=["https://example.com/evil.jpg"])
    assert "images.pexels.com" in str(excinfo.value)


def test_preview_params_rejects_http_scheme() -> None:
    with pytest.raises(ValidationError):
        PreviewMediaParams(thumbnail_urls=["http://images.pexels.com/photos/1/x.jpg"])


def test_preview_params_rejects_file_scheme() -> None:
    with pytest.raises(ValidationError):
        PreviewMediaParams(thumbnail_urls=["file:///etc/passwd"])


def test_preview_params_rejects_empty_list() -> None:
    with pytest.raises(ValidationError):
        PreviewMediaParams(thumbnail_urls=[])


def test_preview_params_rejects_more_than_six() -> None:
    urls = [f"https://images.pexels.com/photos/{i}/x.jpg" for i in range(7)]
    with pytest.raises(ValidationError):
        PreviewMediaParams(thumbnail_urls=urls)


def test_preview_params_accepts_subdomain_only_exact() -> None:
    # Subdomain spoofing attempt should fail (parsed host is "x.images.pexels.com").
    with pytest.raises(ValidationError):
        PreviewMediaParams(thumbnail_urls=["https://x.images.pexels.com/photo.jpg"])


async def test_fetch_thumbnails_returns_image_for_each_ok_url(httpx_mock: HTTPXMock) -> None:
    body = b"\xff\xd8\xff\xe0fakejpegbytes"
    httpx_mock.add_response(
        url=_OK_URL,
        content=body,
        headers={"content-type": "image/jpeg"},
    )
    httpx_mock.add_response(
        url=_OK_URL_2,
        content=body,
        headers={"content-type": "image/jpeg"},
    )
    results = await fetch_thumbnails([_OK_URL, _OK_URL_2])
    assert len(results) == 2
    assert all(r.image is not None for r in results)
    assert all(r.error is None for r in results)
    assert [r.url for r in results] == [_OK_URL, _OK_URL_2]


async def test_fetch_thumbnails_reports_http_errors_inline(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_OK_URL, status_code=404)
    results = await fetch_thumbnails([_OK_URL])
    assert results[0].image is None
    assert "404" in (results[0].error or "")


async def test_fetch_thumbnails_rejects_oversized_body(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_OK_URL,
        content=b"a" * (512 * 1024),
        headers={"content-type": "image/jpeg"},
    )
    results = await fetch_thumbnails([_OK_URL])
    assert results[0].image is None
    assert "exceeds" in (results[0].error or "")
