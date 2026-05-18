"""HTTP client tests using pytest-httpx to mock the Pexels endpoints."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_httpx import HTTPXMock

from pexels_mcp_server.client import (
    PexelsAPIError,
    PexelsAuthError,
    PexelsClient,
    PexelsRateLimitError,
)
from pexels_mcp_server.constants import BASE_URL


def _rate_headers(remaining: int = 19_684, reset: int = 1_700_000_000) -> dict[str, str]:
    return {
        "X-Ratelimit-Limit": "20000",
        "X-Ratelimit-Remaining": str(remaining),
        "X-Ratelimit-Reset": str(reset),
    }


def test_client_requires_api_key() -> None:
    with pytest.raises(PexelsAuthError):
        PexelsClient(api_key="")


def test_client_requires_non_whitespace_api_key() -> None:
    with pytest.raises(PexelsAuthError):
        PexelsClient(api_key="   ")


async def test_search_photos_parses_payload(httpx_mock: HTTPXMock) -> None:
    payload: dict[str, Any] = {
        "page": 1,
        "per_page": 1,
        "total_results": 42,
        "next_page": f"{BASE_URL}/v1/search?page=2",
        "photos": [
            {
                "id": 1,
                "width": 100,
                "height": 200,
                "url": "https://pexels.com/p/1",
                "photographer": "Alice",
                "photographer_url": "https://pexels.com/@alice",
                "photographer_id": 11,
                "avg_color": "#aaa",
                "src": {"original": "https://x/orig.jpg", "large": "https://x/l.jpg"},
                "liked": False,
                "alt": "a cat",
            },
        ],
    }
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/search?query=cat&page=1&per_page=1",
        json=payload,
        headers=_rate_headers(),
    )
    async with PexelsClient(api_key="testkey") as client:
        body, rate = await client.search_photos(query="cat", page=1, per_page=1)
    assert body["total_results"] == 42
    assert rate["remaining"] == 19_684
    assert rate["reset"].endswith("+00:00")
    assert rate["limit"] == 20_000


async def test_get_photo_round_trip(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/photos/42",
        json={"id": 42, "alt": "x", "src": {}, "photographer": "Bob"},
        headers=_rate_headers(remaining=10_000),
    )
    async with PexelsClient(api_key="testkey") as client:
        body, _ = await client.get_photo(42)
    assert body["id"] == 42


async def test_client_raises_on_401(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/search?query=cat&page=1&per_page=15",
        status_code=401,
        json={"error": "unauthorized"},
    )
    async with PexelsClient(api_key="badkey") as client:
        with pytest.raises(PexelsAuthError):
            await client.search_photos(query="cat")


async def test_client_raises_on_429(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/search?query=cat&page=1&per_page=15",
        status_code=429,
        headers={"X-Ratelimit-Reset": "1700000000"},
        json={"error": "rate_limited"},
    )
    async with PexelsClient(api_key="testkey") as client:
        with pytest.raises(PexelsRateLimitError) as excinfo:
            await client.search_photos(query="cat")
    assert excinfo.value.reset_at is not None


async def test_client_retries_on_500_then_succeeds(httpx_mock: HTTPXMock) -> None:
    url = f"{BASE_URL}/v1/curated?page=1&per_page=5"
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    httpx_mock.add_response(
        url=url,
        status_code=200,
        json={"page": 1, "per_page": 5, "total_results": 0, "photos": []},
        headers=_rate_headers(),
    )
    async with PexelsClient(api_key="testkey") as client:
        body, _ = await client.curated_photos(per_page=5)
    assert body["photos"] == []


async def test_client_surfaces_persistent_500(httpx_mock: HTTPXMock) -> None:
    url = f"{BASE_URL}/v1/curated?page=1&per_page=5"
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    async with PexelsClient(api_key="testkey") as client:
        with pytest.raises(PexelsAPIError):
            await client.curated_photos(per_page=5)


async def test_client_drops_none_query_params(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/search?query=cat&page=1&per_page=15",
        json={"page": 1, "per_page": 15, "total_results": 0, "photos": []},
        headers=_rate_headers(),
    )
    async with PexelsClient(api_key="testkey") as client:
        await client.search_photos(query="cat", orientation=None, color=None)
