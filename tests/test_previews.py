"""Tests for the ``PreviewFetcher`` thumbnail loader.

Covers the security boundaries (host allowlist, MIME check, size cap) and
the operational guarantees (cache reuse, FIFO eviction, parallel fetch,
graceful degradation on failure).
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from pexels_mcp_server.previews import PreviewFetcher

_JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00fakejpeg"
_CDN = "https://images.pexels.com/photos/123/test.jpeg"


def _jpeg_response_kwargs(content: bytes = _JPEG_BYTES) -> dict[str, Any]:
    return {
        "content": content,
        "headers": {"content-type": "image/jpeg"},
    }


# --- happy path -----------------------------------------------------------


async def test_fetch_returns_base64_payload(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_CDN, **_jpeg_response_kwargs())
    async with PreviewFetcher() as fetcher:
        result = await fetcher.fetch(_CDN)
    assert result is not None
    assert result.mime_type == "image/jpeg"
    assert base64.b64decode(result.data_base64) == _JPEG_BYTES
    assert result.source_url == _CDN


async def test_fetch_many_preserves_order_and_none_slots(httpx_mock: HTTPXMock) -> None:
    url_a = "https://images.pexels.com/photos/1/a.jpeg"
    url_b = "https://images.pexels.com/photos/2/b.jpeg"
    httpx_mock.add_response(url=url_a, content=b"A" * 30, headers={"content-type": "image/jpeg"})
    httpx_mock.add_response(url=url_b, content=b"B" * 30, headers={"content-type": "image/jpeg"})
    async with PreviewFetcher() as fetcher:
        results = await fetcher.fetch_many([url_a, None, url_b])
    assert len(results) == 3
    assert results[0] is not None
    assert base64.b64decode(results[0].data_base64) == b"A" * 30
    assert results[1] is None
    assert results[2] is not None
    assert base64.b64decode(results[2].data_base64) == b"B" * 30


# --- host allowlist -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example.com/photos/1.jpeg",
        "http://images.pexels.com/photos/1.jpeg",  # plain http rejected
        "https://images.pexels.com.attacker.com/1.jpeg",  # not an exact-match
        "ftp://images.pexels.com/1.jpeg",  # non-https scheme
        "javascript:alert(1)",
        "",
        "not a url",
    ],
)
async def test_fetch_rejects_disallowed_url(url: str) -> None:
    """The allowlist must reject anything that is not exactly
    ``https://images.pexels.com/...``. No outbound HTTP is made on rejection
    — pytest-httpx would fail the test if a stray request reached the mock.
    """
    async with PreviewFetcher() as fetcher:
        result = await fetcher.fetch(url)
    assert result is None


# --- failure modes --------------------------------------------------------


async def test_fetch_returns_none_on_404(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=_CDN, status_code=404)
    async with PreviewFetcher() as fetcher:
        result = await fetcher.fetch(_CDN)
    assert result is None


async def test_fetch_returns_none_on_http_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectTimeout("simulated"))
    async with PreviewFetcher() as fetcher:
        result = await fetcher.fetch(_CDN)
    assert result is None


async def test_fetch_rejects_oversized_response(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_CDN,
        content=b"x" * 501,
        headers={"content-type": "image/jpeg"},
    )
    async with PreviewFetcher(max_bytes=500) as fetcher:
        result = await fetcher.fetch(_CDN)
    assert result is None


async def test_fetch_rejects_non_image_mime(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=_CDN,
        content=b"<html>not an image</html>",
        headers={"content-type": "text/html"},
    )
    async with PreviewFetcher() as fetcher:
        result = await fetcher.fetch(_CDN)
    assert result is None


# --- cache behaviour ------------------------------------------------------


async def test_cache_avoids_second_network_call(httpx_mock: HTTPXMock) -> None:
    """A second fetch for the same URL hits the cache, not the network.

    pytest-httpx fails the test if any registered mock goes unused, so the
    test stays implicit: we only register one response and call ``fetch``
    twice. The second call must succeed without consuming a second mock.
    """
    httpx_mock.add_response(url=_CDN, **_jpeg_response_kwargs())
    async with PreviewFetcher() as fetcher:
        first = await fetcher.fetch(_CDN)
        second = await fetcher.fetch(_CDN)
    assert first is not None
    assert second is not None
    assert second.data_base64 == first.data_base64


async def test_cache_fifo_evicts_oldest_entry(httpx_mock: HTTPXMock) -> None:
    """When the cache is full, inserting a new entry evicts the oldest."""
    url_a = "https://images.pexels.com/photos/1/a.jpeg"
    url_b = "https://images.pexels.com/photos/2/b.jpeg"
    url_c = "https://images.pexels.com/photos/3/c.jpeg"
    for url in (url_a, url_b, url_c):
        httpx_mock.add_response(url=url, **_jpeg_response_kwargs())
    async with PreviewFetcher(cache_max_entries=2) as fetcher:
        await fetcher.fetch(url_a)
        await fetcher.fetch(url_b)
        await fetcher.fetch(url_c)  # evicts url_a
        # url_a was evicted, so a second fetch goes to the network again.
        httpx_mock.add_response(url=url_a, **_jpeg_response_kwargs())
        result = await fetcher.fetch(url_a)
    assert result is not None


async def test_cache_respects_ttl(httpx_mock: HTTPXMock) -> None:
    """A cached entry that is older than ``cache_ttl_seconds`` is refreshed."""
    httpx_mock.add_response(url=_CDN, **_jpeg_response_kwargs())
    async with PreviewFetcher(cache_ttl_seconds=0) as fetcher:
        first = await fetcher.fetch(_CDN)
        assert first is not None
        # TTL=0 means every subsequent fetch goes back to the network.
        httpx_mock.add_response(url=_CDN, **_jpeg_response_kwargs())
        second = await fetcher.fetch(_CDN)
    assert second is not None
