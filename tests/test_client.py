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
            await client.search_photos(api_key=None, query="cat")
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
        url=f"{BASE_URL}/v1/search?query=cat&page=1&per_page=15",
        status_code=403,
        json={"error": "forbidden"},
    )
    async with PexelsClient() as client:
        with pytest.raises(PexelsAuthError):
            await client.search_photos(api_key="restricted", query="cat")


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
    url = f"{BASE_URL}/v1/search?query=cat&page=1&per_page=5"
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    httpx_mock.add_response(
        url=url,
        status_code=200,
        json={"page": 1, "per_page": 5, "total_results": 0, "photos": []},
        headers=_rate_headers(),
    )
    async with PexelsClient() as client:
        body, _ = await client.search_photos(api_key="testkey", query="cat", per_page=5)
    assert body["photos"] == []


async def test_client_surfaces_persistent_500(httpx_mock: HTTPXMock) -> None:
    url = f"{BASE_URL}/v1/search?query=cat&page=1&per_page=5"
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    async with PexelsClient() as client:
        with pytest.raises(PexelsAPIError):
            await client.search_photos(api_key="testkey", query="cat", per_page=5)


async def test_client_drops_none_query_params(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/search?query=cat&page=1&per_page=15",
        json={"page": 1, "per_page": 15, "total_results": 0, "photos": []},
        headers=_rate_headers(),
    )
    async with PexelsClient() as client:
        await client.search_photos(api_key="testkey", query="cat", orientation=None, color=None)


# --- PexelsAPIError sanitization -----------------------------------------
# Upstream response bodies are projected to a one-line agent-safe string
# so a hostile / malformed Pexels response cannot smuggle control chars
# or echo the caller's key into the LLM context.


def test_pexels_api_error_strips_control_chars() -> None:
    err = PexelsAPIError(500, "boom\x00\x07\nwith\ttab")
    assert "\x00" not in str(err)
    assert "\x07" not in str(err)
    assert "boom" in str(err)


def test_pexels_api_error_redacts_token_shaped_strings() -> None:
    leaked = "x" * 56  # Pexels API keys are 56 chars of [A-Za-z0-9].
    err = PexelsAPIError(403, f"forbidden for key {leaked} please rotate")
    assert leaked not in str(err)
    assert "<redacted>" in str(err)


def test_pexels_api_error_caps_message_length() -> None:
    err = PexelsAPIError(502, "x " * 1000)
    assert len(str(err)) < 250


# --- validate_key (BYOK setup probe) ------------------------------------
#
# ``validate_key`` hits ``/v1/collections`` (the caller's own collections)
# and *not* ``/v1/curated`` or ``/v1/search`` — the latter two are served
# from the Pexels CDN and respond 200 with cached content even for an
# invalid key, which makes them useless for authentication checks.


async def test_validate_key_returns_true_on_200(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/collections?per_page=1",
        json={"page": 1, "per_page": 1, "total_results": 0, "collections": []},
        headers=_rate_headers(),
        match_headers={"Authorization": "good-key"},
    )
    async with PexelsClient() as client:
        assert await client.validate_key("good-key") is True


async def test_validate_key_returns_false_on_401(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/collections?per_page=1",
        status_code=401,
        json={"status": 401, "code": "Unauthorized", "message": "Invalid API key"},
    )
    async with PexelsClient() as client:
        assert await client.validate_key("bad-key") is False


async def test_validate_key_returns_false_on_403(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/collections?per_page=1",
        status_code=403,
        json={"error": "forbidden"},
    )
    async with PexelsClient() as client:
        assert await client.validate_key("revoked-key") is False


async def test_validate_key_raises_on_persistent_5xx(httpx_mock: HTTPXMock) -> None:
    """A 5xx from Pexels is a service problem, not a key problem — surface it
    so the /setup handler can show 'try again in a moment' instead of
    blaming the user's key."""
    url = f"{BASE_URL}/v1/collections?per_page=1"
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    httpx_mock.add_response(url=url, status_code=500, json={"error": "boom"})
    async with PexelsClient() as client:
        with pytest.raises(PexelsAPIError):
            await client.validate_key("any-key")
