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


async def test_methods_reject_empty_api_key() -> None:
    async with PexelsClient() as client:
        with pytest.raises(PexelsAuthError):
            await client.search_photos(api_key="", query="cat")
        with pytest.raises(PexelsAuthError):
            await client.curated_photos(api_key=None)
        with pytest.raises(PexelsAuthError):
            await client.search_photos(api_key="   ", query="cat")


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
        match_headers={"Authorization": "testkey"},
    )
    async with PexelsClient() as client:
        body, rate = await client.search_photos(api_key="testkey", query="cat", page=1, per_page=1)
    assert body["total_results"] == 42
    assert rate["remaining"] == 19_684
    assert rate["reset"].endswith("+00:00")
    assert rate["limit"] == 20_000


async def test_per_call_key_overrides_per_request_header(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/photos/42",
        json={"id": 42, "alt": "x", "src": {}, "photographer": "Bob"},
        headers=_rate_headers(remaining=10_000),
        match_headers={"Authorization": "trimmed_key"},
    )
    async with PexelsClient() as client:
        body, _ = await client.get_photo(42, api_key="  trimmed_key  ")
    assert body["id"] == 42


async def test_client_raises_on_401(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/search?query=cat&page=1&per_page=15",
        status_code=401,
        json={"error": "unauthorized"},
    )
    async with PexelsClient() as client:
        with pytest.raises(PexelsAuthError):
            await client.search_photos(api_key="badkey", query="cat")


async def test_client_raises_on_403(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/curated?page=1&per_page=15",
        status_code=403,
        json={"error": "forbidden"},
    )
    async with PexelsClient() as client:
        with pytest.raises(PexelsAuthError):
            await client.curated_photos(api_key="restricted")


async def test_client_raises_on_429(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/search?query=cat&page=1&per_page=15",
        status_code=429,
        headers={"X-Ratelimit-Reset": "1700000000"},
        json={"error": "rate_limited"},
    )
    async with PexelsClient() as client:
        with pytest.raises(PexelsRateLimitError) as excinfo:
            await client.search_photos(api_key="testkey", query="cat")
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
    async with PexelsClient() as client:
        body, _ = await client.curated_photos(api_key="testkey", per_page=5)
    assert body["photos"] == []


async def test_client_surfaces_persistent_500(httpx_mock: HTTPXMock) -> None:
    url = f"{BASE_URL}/v1/curated?page=1&per_page=5"
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    async with PexelsClient() as client:
        with pytest.raises(PexelsAPIError):
            await client.curated_photos(api_key="testkey", per_page=5)


async def test_list_my_collections_targets_collections_root(httpx_mock: HTTPXMock) -> None:
    # GET /v1/collections (the bare root, NOT /v1/collections/featured) returns
    # the collections owned by the API key holder.
    payload = {
        "page": 1,
        "per_page": 15,
        "total_results": 2,
        "collections": [
            {"id": "abc", "title": "My moodboard", "private": False},
            {"id": "def", "title": "Hidden picks", "private": True},
        ],
    }
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/collections?page=1&per_page=15",
        json=payload,
        headers=_rate_headers(),
        match_headers={"Authorization": "testkey"},
    )
    async with PexelsClient() as client:
        body, rate = await client.list_my_collections(api_key="testkey")
    assert body["total_results"] == 2
    assert body["collections"][0]["id"] == "abc"
    assert rate["limit"] == 20_000


async def test_client_drops_none_query_params(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/search?query=cat&page=1&per_page=15",
        json={"page": 1, "per_page": 15, "total_results": 0, "photos": []},
        headers=_rate_headers(),
    )
    async with PexelsClient() as client:
        await client.search_photos(api_key="testkey", query="cat", orientation=None, color=None)


# --- validate_key (BYOK setup probe) ------------------------------------


async def test_validate_key_returns_true_on_200(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/curated?per_page=1",
        json={"page": 1, "per_page": 1, "total_results": 0, "photos": []},
        headers=_rate_headers(),
        match_headers={"Authorization": "good-key"},
    )
    async with PexelsClient() as client:
        assert await client.validate_key("good-key") is True


async def test_validate_key_returns_false_on_401(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/curated?per_page=1",
        status_code=401,
        json={"error": "unauthorized"},
    )
    async with PexelsClient() as client:
        assert await client.validate_key("bad-key") is False


async def test_validate_key_returns_false_on_403(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/curated?per_page=1",
        status_code=403,
        json={"error": "forbidden"},
    )
    async with PexelsClient() as client:
        assert await client.validate_key("revoked-key") is False


async def test_validate_key_raises_on_persistent_5xx(httpx_mock: HTTPXMock) -> None:
    """A 5xx from Pexels is a service problem, not a key problem — surface it
    so the /setup handler can show 'try again in a moment' instead of
    blaming the user's key."""
    url = f"{BASE_URL}/v1/curated?per_page=1"
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    async with PexelsClient() as client:
        with pytest.raises(PexelsAPIError):
            await client.validate_key("any-key")
