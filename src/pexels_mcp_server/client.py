"""Async Pexels HTTP client.

Wraps the Pexels REST endpoints we expose as tools. Each public method takes
an ``api_key`` argument resolved by the caller (``server.py`` reads the
per-request ``X-Pexels-Api-Key`` header in HTTP mode or the ``PEXELS_API_KEY``
env var in stdio mode). The client itself never stores a key.

All methods return a ``(payload, rate_limit)`` tuple so the tool layer can
stitch the rate-limit envelope onto every response.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any

import httpx

from .constants import (
    BASE_URL,
    COLLECTIONS_PREFIX,
    HTTP_TIMEOUT_SECONDS,
    PHOTOS_PREFIX,
    RETRY_BACKOFF_SECONDS,
    USER_AGENT,
    VIDEOS_PREFIX,
)


def _backoff_delay() -> float:
    """Compute a jittered backoff delay for the single retry path.

    The previous fixed 1.0s blocked the event loop hot path during a Pexels
    5xx. With bursty AI traffic (5-30 calls per session) this stalled the
    whole session. A short base + jitter keeps retries useful without
    starving other coroutines.
    """
    return RETRY_BACKOFF_SECONDS * (0.25 + random.random() * 0.5)


logger = logging.getLogger("pexels_mcp_server.client")


class PexelsAuthError(RuntimeError):
    """Raised when the API key is missing or rejected by Pexels."""


class PexelsRateLimitError(RuntimeError):
    """Raised when the API returns HTTP 429."""

    def __init__(self, reset_at: str | None) -> None:
        suffix = f" Resets at {reset_at}." if reset_at else ""
        super().__init__(f"Pexels rate limit exceeded.{suffix} Reduce request frequency.")
        self.reset_at = reset_at


class PexelsAPIError(RuntimeError):
    """Raised for any other non-success response from Pexels."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Pexels API error {status_code}: {message}")
        self.status_code = status_code


_MISSING_KEY_MESSAGE = (
    "Pexels API key is missing. "
    "Send it as the 'X-Pexels-Api-Key' header on the MCP request "
    "(HTTP transport) or set the PEXELS_API_KEY env var (stdio transport). "
    "Get a key at https://www.pexels.com/api/"
)


def _parse_rate_limit(headers: httpx.Headers) -> dict[str, Any]:
    """Parse the ``X-Ratelimit-*`` headers into a JSON-friendly dict."""
    limit = headers.get("X-Ratelimit-Limit")
    remaining = headers.get("X-Ratelimit-Remaining")
    reset = headers.get("X-Ratelimit-Reset")
    result: dict[str, Any] = {}
    if limit is not None:
        try:
            result["limit"] = int(limit)
        except ValueError:
            result["limit"] = limit
    if remaining is not None:
        try:
            result["remaining"] = int(remaining)
        except ValueError:
            result["remaining"] = remaining
    if reset is not None:
        try:
            ts = int(reset)
            result["reset"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            result["reset_epoch"] = ts
        except ValueError:
            result["reset"] = reset
    return result


def _drop_none(params: dict[str, Any]) -> dict[str, Any]:
    """Strip keys whose value is None so we do not send empty query strings."""
    return {k: v for k, v in params.items() if v is not None}


class PexelsClient:
    """Thin async wrapper around the Pexels REST API.

    The client maintains a single ``httpx.AsyncClient`` (connection pool,
    HTTP/2 multiplexing) and signs each request with whatever ``api_key`` the
    caller supplies. There is no stored default key: callers must resolve the
    effective key themselves before invoking a method.
    """

    def __init__(self, *, timeout: float = HTTP_TIMEOUT_SECONDS) -> None:
        # ``api.pexels.com`` negotiates HTTP/2 via ALPN. One TLS connection
        # is then reused across every concurrent tool call this process
        # serves, multiplexing requests on a single stream so:
        #   - The TLS handshake cost is paid once (~80-150 ms saved on each
        #     reused connection vs HTTP/1.1 + new connection).
        #   - Concurrent requests no longer block on the keepalive pool size
        #     when many MCP users hit the server at once — they share the
        #     one HTTP/2 connection until Pexels caps stream concurrency
        #     (usually 100, far above any realistic per-process load).
        # If Pexels ever stops advertising h2 over ALPN, httpx silently
        # falls back to HTTP/1.1 — no behaviour change for callers.
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            http2=True,
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=60,
            ),
        )

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    async def __aenter__(self) -> PexelsClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @staticmethod
    def _require_key(api_key: str | None) -> str:
        if not api_key or not api_key.strip():
            raise PexelsAuthError(_MISSING_KEY_MESSAGE)
        return api_key.strip()

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None,
        *,
        api_key: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Issue a GET request with one retry on 5xx errors."""
        query = _drop_none(params or {})
        headers = {"Authorization": api_key}
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                response = await self._client.get(path, params=query, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == 1:
                    logger.warning("Pexels request to %s failed (%s). Retrying once.", path, exc)
                    await asyncio.sleep(_backoff_delay())
                    continue
                raise PexelsAPIError(0, f"network error: {exc}") from exc

            rate_limit = _parse_rate_limit(response.headers)
            remaining = rate_limit.get("remaining")
            if isinstance(remaining, int) and remaining < 100:
                logger.warning(
                    "Pexels rate limit low: %s requests left (reset %s)",
                    remaining,
                    rate_limit.get("reset"),
                )

            if response.status_code == httpx.codes.OK:
                return response.json(), rate_limit
            if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
                raise PexelsAuthError(_MISSING_KEY_MESSAGE)
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                raise PexelsRateLimitError(rate_limit.get("reset"))
            if 500 <= response.status_code < 600 and attempt == 1:
                logger.warning("Pexels returned %s. Retrying once.", response.status_code)
                await asyncio.sleep(_backoff_delay())
                continue
            raise PexelsAPIError(response.status_code, response.text[:300])

        raise PexelsAPIError(0, f"retry exhausted: {last_exc}")

    async def search_photos(
        self,
        *,
        api_key: str | None,
        query: str,
        orientation: str | None = None,
        size: str | None = None,
        color: str | None = None,
        locale: str | None = None,
        page: int = 1,
        per_page: int = 15,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return await self._request(
            f"{PHOTOS_PREFIX}/search",
            {
                "query": query,
                "orientation": orientation,
                "size": size,
                "color": color,
                "locale": locale,
                "page": page,
                "per_page": per_page,
            },
            api_key=self._require_key(api_key),
        )

    async def curated_photos(
        self,
        *,
        api_key: str | None,
        page: int = 1,
        per_page: int = 15,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return await self._request(
            f"{PHOTOS_PREFIX}/curated",
            {"page": page, "per_page": per_page},
            api_key=self._require_key(api_key),
        )

    async def get_photo(
        self, photo_id: int, *, api_key: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return await self._request(
            f"{PHOTOS_PREFIX}/photos/{photo_id}",
            None,
            api_key=self._require_key(api_key),
        )

    async def search_videos(
        self,
        *,
        api_key: str | None,
        query: str,
        orientation: str | None = None,
        size: str | None = None,
        locale: str | None = None,
        page: int = 1,
        per_page: int = 15,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return await self._request(
            f"{VIDEOS_PREFIX}/search",
            {
                "query": query,
                "orientation": orientation,
                "size": size,
                "locale": locale,
                "page": page,
                "per_page": per_page,
            },
            api_key=self._require_key(api_key),
        )

    async def popular_videos(
        self,
        *,
        api_key: str | None,
        min_width: int | None = None,
        min_height: int | None = None,
        min_duration: int | None = None,
        max_duration: int | None = None,
        page: int = 1,
        per_page: int = 15,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return await self._request(
            f"{VIDEOS_PREFIX}/popular",
            {
                "min_width": min_width,
                "min_height": min_height,
                "min_duration": min_duration,
                "max_duration": max_duration,
                "page": page,
                "per_page": per_page,
            },
            api_key=self._require_key(api_key),
        )

    async def get_video(
        self, video_id: int, *, api_key: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        # Pexels exposes the single-video endpoint at /videos/videos/:id. The
        # repeated "videos" segment is intentional per the official docs:
        # https://www.pexels.com/api/documentation/#videos-show
        return await self._request(
            f"{VIDEOS_PREFIX}/videos/{video_id}",
            None,
            api_key=self._require_key(api_key),
        )

    async def list_featured_collections(
        self,
        *,
        api_key: str | None,
        page: int = 1,
        per_page: int = 15,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return await self._request(
            f"{COLLECTIONS_PREFIX}/featured",
            {"page": page, "per_page": per_page},
            api_key=self._require_key(api_key),
        )

    async def list_my_collections(
        self,
        *,
        api_key: str | None,
        page: int = 1,
        per_page: int = 15,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        # GET /v1/collections returns the collections owned by the API key
        # holder. Same Authorization scheme as every other endpoint; the
        # response shape mirrors /v1/collections/featured so format_collection_list
        # can be reused.
        return await self._request(
            COLLECTIONS_PREFIX,
            {"page": page, "per_page": per_page},
            api_key=self._require_key(api_key),
        )

    async def get_collection_media(
        self,
        *,
        api_key: str | None,
        collection_id: str,
        type: str | None = None,
        sort: str | None = None,
        page: int = 1,
        per_page: int = 15,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return await self._request(
            f"{COLLECTIONS_PREFIX}/{collection_id}",
            {
                "type": type,
                "sort": sort,
                "page": page,
                "per_page": per_page,
            },
            api_key=self._require_key(api_key),
        )

    async def validate_key(self, api_key: str) -> bool:
        """Probe ``GET /v1/curated`` with ``api_key`` to confirm it works.

        Used by the /setup form to give immediate feedback when the user
        pastes a wrong or expired key. The endpoint is the cheapest
        authenticated Pexels endpoint (no required query params, returns
        a single curated photo when called with ``per_page=1``).

        Returns ``True`` on HTTP 200, ``False`` on 401/403, and raises
        :class:`PexelsAPIError` on anything else (network failure, 5xx) so
        the caller can distinguish "key is bad" from "Pexels is down".
        """
        try:
            await self._request(
                f"{PHOTOS_PREFIX}/curated",
                {"per_page": 1},
                api_key=self._require_key(api_key),
            )
        except PexelsAuthError:
            return False
        return True
