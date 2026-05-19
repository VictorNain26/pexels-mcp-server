"""Thumbnail fetcher backing the ``pexels_preview_media`` tool.

The MCP layer hands us a list of CDN URLs (typically the ``thumbnail_url`` or
``preview_image_url`` returned by an earlier search). We fetch each one
concurrently, cap the body size, and wrap the bytes into ``mcp.server.fastmcp``
``Image`` helpers so FastMCP can convert them to ``ImageContent`` blocks.

All URL validation happens via a Pydantic field validator in ``schemas.py``.
This module only performs network I/O on already-trusted hosts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from mcp.server.fastmcp.utilities.types import Image

from .constants import (
    PREVIEW_FETCH_TIMEOUT_SECONDS,
    PREVIEW_MAX_BYTES,
    USER_AGENT,
)

logger = logging.getLogger("pexels_mcp_server.previews")


@dataclass(frozen=True)
class PreviewResult:
    """Outcome of fetching a single thumbnail."""

    url: str
    image: Image | None
    error: str | None


_FORMAT_FROM_MIME = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


async def _fetch_one(client: httpx.AsyncClient, url: str) -> PreviewResult:
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        return PreviewResult(url=url, image=None, error=f"network error: {exc}")
    if response.status_code != httpx.codes.OK:
        return PreviewResult(
            url=url,
            image=None,
            error=f"HTTP {response.status_code}",
        )
    body = response.content
    if len(body) > PREVIEW_MAX_BYTES:
        return PreviewResult(
            url=url,
            image=None,
            error=f"thumbnail exceeds {PREVIEW_MAX_BYTES} bytes",
        )
    mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
    fmt = _FORMAT_FROM_MIME.get(mime, "jpeg")
    return PreviewResult(url=url, image=Image(data=body, format=fmt), error=None)


async def fetch_thumbnails(urls: list[str]) -> list[PreviewResult]:
    """Fetch a batch of CDN thumbnails concurrently. Order is preserved.

    Redirects are intentionally disabled: the URL allowlist runs at the
    schema layer on the *initial* host only, so a CDN redirect to an
    arbitrary location would bypass it. In practice ``images.pexels.com``
    does not redirect; an unexpected 3xx becomes a tool-facing error
    instead of a silent SSRF vector.
    """
    async with httpx.AsyncClient(
        timeout=PREVIEW_FETCH_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=False,
    ) as client:
        coroutines = [_fetch_one(client, url) for url in urls]
        return await asyncio.gather(*coroutines)
