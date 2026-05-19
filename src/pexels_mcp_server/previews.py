"""Async fetcher for Pexels CDN thumbnails returned to MCP clients as inline images.

The search and list tools embed a per-result thumbnail as an MCP
``ImageContent`` block so vision-capable clients (Claude with multimodal,
ChatGPT desktop) render the image directly in the conversation and the
agent can run a vision-based pick on top of Pexels' relevance ranking.

Design constraints
------------------

- **Host allowlist**: outbound fetches MUST target ``images.pexels.com``
  only. Validated at the boundary of every public method to keep the
  SSRF surface tight.
- **Bounded concurrency**: a process-wide ``asyncio.Semaphore`` caps how
  many CDN fetches run at once so a 15-photo search cannot saturate the
  httpx pool or trigger the Pexels CDN's burst quota.
- **Per-fetch timeout + size limit**: a hung or malicious response cannot
  block a tool call beyond ``timeout_seconds`` or exceed ``max_bytes``.
- **Best-effort**: a fetch failure (timeout, 4xx, oversized, wrong host)
  is logged and the slot is skipped — the rest of the response still
  ships. The agent gets the URL back in the caption text and can render
  it manually if its client supports it.
- **LRU-ish cache**: the same thumbnail URL is fetched at most once per
  ``cache_ttl_seconds`` window. FIFO eviction when the cache exceeds
  ``cache_max_entries`` to bound memory under bot churn.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

import httpx

from .constants import USER_AGENT

logger = logging.getLogger("pexels_mcp_server.previews")

# Pexels serves all photo and video preview frames from this host. We do
# not accept any other origin — that keeps the outbound HTTP from being
# usable as an SSRF probe against the operator's internal network.
_ALLOWED_HOST: Final[str] = "images.pexels.com"

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0
# 500 KB is generous for a ``medium`` Pexels thumbnail (~50-150 KB JPEG);
# the cap stops a malicious CDN response from being used to balloon memory.
_DEFAULT_MAX_BYTES: Final[int] = 500_000
# Up to 12 fetches in flight at once. 15-photo searches finish in two
# rounds with this cap; bumping higher would saturate the eco-nano CPU.
_DEFAULT_CONCURRENCY: Final[int] = 12
_DEFAULT_CACHE_MAX_ENTRIES: Final[int] = 256
_DEFAULT_CACHE_TTL_SECONDS: Final[float] = 600.0


@dataclass(frozen=True)
class PreviewImage:
    """A single fetched thumbnail, ready to ship as MCP ``ImageContent``."""

    data_base64: str
    mime_type: str
    source_url: str


class PreviewFetcher:
    """Async, bounded, cached fetcher for Pexels CDN thumbnails."""

    def __init__(
        self,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        concurrency: int = _DEFAULT_CONCURRENCY,
        cache_max_entries: int = _DEFAULT_CACHE_MAX_ENTRIES,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._max_bytes = max_bytes
        self._cache_max_entries = cache_max_entries
        self._cache_ttl_seconds = cache_ttl_seconds
        self._semaphore = asyncio.Semaphore(concurrency)
        # ``follow_redirects=False`` is intentional: the allowlist runs on
        # the initial URL only; a redirect to an arbitrary location would
        # bypass it. images.pexels.com does not redirect in normal use.
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            http2=True,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=60,
            ),
        )
        # Cache value: (PreviewImage, inserted_at). FIFO eviction on
        # insertion when the dict grows past ``cache_max_entries``.
        self._cache: dict[str, tuple[PreviewImage, float]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PreviewFetcher:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def _check_url(self, url: str) -> bool:
        """Reject anything that isn't an https URL on ``images.pexels.com``.

        Validated at the boundary of every fetch so a tool handler that
        accidentally threads through a Pexels response field other than
        ``src.medium`` (e.g. a future ``url`` field on a tag) cannot
        trigger an outbound call to an arbitrary host.
        """
        try:
            parsed = urlparse(url)
        except (ValueError, TypeError):
            return False
        if parsed.scheme != "https":
            return False
        return parsed.hostname == _ALLOWED_HOST

    def _cache_get(self, url: str, now: float) -> PreviewImage | None:
        entry = self._cache.get(url)
        if entry is None:
            return None
        image, inserted_at = entry
        # ``>=`` so a ``cache_ttl_seconds=0`` config means "do not cache",
        # which is the least surprising interpretation for tests / disabled
        # mode. With ``>`` a fresh-inserted entry would still hit on the
        # same monotonic tick.
        if now - inserted_at >= self._cache_ttl_seconds:
            del self._cache[url]
            return None
        return image

    def _cache_put(self, url: str, image: PreviewImage, now: float) -> None:
        # FIFO eviction when the cache is full. dict preserves insertion
        # order in CPython 3.7+, so the first key is the oldest entry.
        if url not in self._cache and len(self._cache) >= self._cache_max_entries:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        self._cache[url] = (image, now)

    async def fetch(self, url: str) -> PreviewImage | None:
        """Fetch one thumbnail. Returns ``None`` on any failure path."""
        if not self._check_url(url):
            logger.warning("Preview URL rejected by allowlist: %s", url)
            return None
        now = time.monotonic()
        cached = self._cache_get(url, now)
        if cached is not None:
            return cached
        async with self._semaphore:
            try:
                response = await self._client.get(url)
            except httpx.HTTPError as exc:
                logger.warning("Preview fetch failed for %s: %s", url, exc)
                return None
        if response.status_code != httpx.codes.OK:
            logger.warning("Preview fetch returned %s for %s", response.status_code, url)
            return None
        content = response.content
        if len(content) > self._max_bytes:
            logger.warning("Preview oversized (%d > %d) for %s", len(content), self._max_bytes, url)
            return None
        mime_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if not mime_type.startswith("image/"):
            logger.warning("Preview MIME not image/* (%s) for %s", mime_type, url)
            return None
        image = PreviewImage(
            data_base64=base64.b64encode(content).decode("ascii"),
            mime_type=mime_type,
            source_url=url,
        )
        self._cache_put(url, image, now)
        return image

    async def fetch_many(self, urls: list[str | None]) -> list[PreviewImage | None]:
        """Fetch many thumbnails in parallel, preserving caller order.

        ``None`` entries in ``urls`` (a missing ``src.medium`` on a Pexels
        item) round-trip as ``None`` results so the caller can pair them
        one-to-one with the original payload items.
        """
        tasks = [self.fetch(u) if u else _none_async() for u in urls]
        return list(await asyncio.gather(*tasks))


async def _none_async() -> None:
    return None
