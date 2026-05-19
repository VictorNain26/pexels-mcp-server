"""Live integration tests hitting the real Pexels REST API + CDN.

Opted out of CI (``addopts = "-m 'not live'"`` in pyproject) and skipped
when ``PEXELS_API_KEY`` is unset so a clean checkout never accidentally
spends quota.

To run locally::

    PEXELS_API_KEY=<your key> uv run pytest -m live -v

These tests verify the end-to-end flow against the real Pexels surface
the unit tests mock: ``api.pexels.com`` REST calls, the ``X-Ratelimit-*``
header parsing, the thumbnail fetch against ``images.pexels.com``, the
post-hoc aspect-ratio + min_width filters.
"""

from __future__ import annotations

import os

import pytest

from pexels_mcp_server.client import PexelsClient
from pexels_mcp_server.formatters import filter_by_dimensions, photo_preview_url
from pexels_mcp_server.previews import PreviewFetcher

pytestmark = pytest.mark.live

_KEY = os.environ.get("PEXELS_API_KEY", "").strip()


def _require_key() -> str:
    if not _KEY:
        pytest.skip("PEXELS_API_KEY not set; live tests skipped.")
    return _KEY


async def test_live_search_photos_returns_results() -> None:
    key = _require_key()
    async with PexelsClient() as client:
        body, rate = await client.search_photos(api_key=key, query="office", per_page=3)
    assert "photos" in body
    assert len(body["photos"]) <= 3
    assert all(p.get("id") and p.get("photographer") for p in body["photos"])
    # Pexels documents the rate-limit headers on every authenticated call.
    assert "limit" in rate
    assert "remaining" in rate


async def test_live_search_with_aspect_ratio_filter_excludes_off_ratio() -> None:
    """Sanity check the post-hoc aspect-ratio filter against real items.

    We ask for 30 candidates, then keep only the ones that match 16:9
    within 5 %. Every kept item must satisfy the ratio constraint.
    """
    key = _require_key()
    async with PexelsClient() as client:
        body, _ = await client.search_photos(api_key=key, query="city", per_page=30)
    filtered = filter_by_dimensions(body.get("photos") or [], aspect_ratio=16 / 9)
    for photo in filtered:
        ratio = photo["width"] / photo["height"]
        assert abs(ratio - 16 / 9) <= (16 / 9) * 0.05


async def test_live_search_with_min_width_excludes_smaller_assets() -> None:
    key = _require_key()
    async with PexelsClient() as client:
        body, _ = await client.search_photos(api_key=key, query="office", per_page=20)
    filtered = filter_by_dimensions(body.get("photos") or [], min_width=4000)
    for photo in filtered:
        assert photo["width"] >= 4000


async def test_live_thumbnail_fetch_against_real_cdn() -> None:
    """End-to-end: search a real photo, fetch its medium thumbnail from
    images.pexels.com, confirm the body decodes as an image."""
    key = _require_key()
    async with PexelsClient() as client:
        body, _ = await client.search_photos(api_key=key, query="office", per_page=1)
    photos = body.get("photos") or []
    assert photos, "Pexels returned no photos for 'office' — Pexels outage?"
    url = photo_preview_url(photos[0])
    assert url is not None
    assert url.startswith("https://images.pexels.com/")
    async with PreviewFetcher() as fetcher:
        preview = await fetcher.fetch(url)
    assert preview is not None
    assert preview.mime_type.startswith("image/")
    assert len(preview.data_base64) > 100  # ~real JPEG, not a 1-pixel pixel


async def test_live_validate_key_accepts_real_key() -> None:
    key = _require_key()
    async with PexelsClient() as client:
        assert await client.validate_key(key) is True


async def test_live_validate_key_rejects_garbage_key() -> None:
    _require_key()  # only run if real key set (so the test is meaningful)
    async with PexelsClient() as client:
        assert await client.validate_key("definitely-not-a-real-key-zzz") is False
