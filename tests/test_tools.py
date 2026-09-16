"""In-process tests for the tool, resource and prompt handlers in ``server.py``.

Each test drives the module-level FastMCP instance through a real MCP
``ClientSession`` over the SDK's in-memory transport
(``mcp.shared.memory``), so argument parsing, the lifespan-owned
``PexelsClient``, ``_sdk_patches`` and ``isError`` wrapping all run. The
Pexels REST API is mocked with pytest-httpx.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import ModuleType
from typing import Any

import pytest
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent, TextResourceContents
from pydantic import AnyUrl
from pytest_httpx import HTTPXMock

from pexels_mcp_server.constants import BASE_URL
from pexels_mcp_server.transport import pexels_key_ctx

_API_KEY = "test-pexels-key"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("TRANSPORT", "stdio")
    monkeypatch.setenv("PEXELS_API_KEY", _API_KEY)
    return importlib.import_module("pexels_mcp_server.server")


@asynccontextmanager
async def _session(server: ModuleType) -> AsyncIterator[ClientSession]:
    async with create_connected_server_and_client_session(server.mcp) as session:
        yield session


def _photo(photo_id: int, width: int = 4000, height: int = 3000) -> dict[str, Any]:
    return {
        "id": photo_id,
        "width": width,
        "height": height,
        "url": f"https://www.pexels.com/photo/p-{photo_id}/",
        "photographer": "Alice",
        "photographer_url": "https://www.pexels.com/@alice",
        "photographer_id": 7,
        "avg_color": "#AAAAAA",
        "src": {"original": f"https://images.pexels.com/photos/{photo_id}/original.jpeg"},
        "liked": False,
        "alt": f"photo {photo_id}",
    }


def _video(video_id: int, width: int = 3840, height: int = 2160) -> dict[str, Any]:
    return {
        "id": video_id,
        "width": width,
        "height": height,
        "url": f"https://www.pexels.com/video/v-{video_id}/",
        "duration": 12,
        "user": {"name": "Bob", "url": "https://www.pexels.com/@bob"},
        "video_files": [
            {"quality": "sd", "width": 640, "height": 360, "link": "https://videos/sd.mp4"},
            {"quality": "uhd", "width": width, "height": height, "link": "https://videos/uhd.mp4"},
        ],
    }


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert result.isError is False, result.content
    assert result.structuredContent is not None
    text = result.content[0]
    assert isinstance(text, TextContent)
    assert json.loads(text.text) == result.structuredContent
    return result.structuredContent


def _error_text(result: CallToolResult) -> str:
    assert result.isError is True
    text = result.content[0]
    assert isinstance(text, TextContent)
    return text.text


def _resource_json(contents: list[Any]) -> dict[str, Any]:
    assert len(contents) == 1
    item = contents[0]
    assert isinstance(item, TextResourceContents)
    assert item.mimeType == "application/json"
    decoded: dict[str, Any] = json.loads(item.text)
    return decoded


async def test_lists_eight_read_only_tools(server: ModuleType) -> None:
    async with _session(server) as session:
        tools = (await session.list_tools()).tools

    assert sorted(t.name for t in tools) == [
        "pexels_get_collection_media",
        "pexels_get_curated_photos",
        "pexels_get_featured_collections",
        "pexels_get_photo",
        "pexels_get_popular_videos",
        "pexels_get_video",
        "pexels_search_photos",
        "pexels_search_videos",
    ]
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.outputSchema is not None


async def test_search_photos_forwards_native_filters(
    server: ModuleType, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=(
            f"{BASE_URL}/v1/search?query=paris&orientation=landscape&size=large"
            "&color=blue&locale=fr-FR&page=2&per_page=2"
        ),
        match_headers={"Authorization": _API_KEY},
        json={
            "page": 2,
            "per_page": 2,
            "total_results": 40,
            "next_page": f"{BASE_URL}/v1/search?page=3",
            "photos": [_photo(1), _photo(2)],
        },
    )
    async with _session(server) as session:
        result = await session.call_tool(
            "pexels_search_photos",
            {
                "query": "paris",
                "orientation": "landscape",
                "size": "large",
                "color": "BLUE",
                "locale": "fr-FR",
                "page": 2,
                "per_page": 2,
            },
        )

    body = _structured(result)
    assert body["page"] == 2
    assert body["count"] == 2
    assert body["has_more"] is True
    assert body["next_page"] == 3
    assert body["total_results"] == 40
    assert body["photos"][0] == {
        "id": 1,
        "alt": "photo 1",
        "page_url": "https://www.pexels.com/photo/p-1/",
        "photographer": "Alice",
        "photographer_url": "https://www.pexels.com/@alice",
        "width": 4000,
        "height": 3000,
        "image_url": "https://images.pexels.com/photos/1/original.jpeg",
    }
    assert "filter_diagnostics" not in body


async def test_search_photos_oversamples_and_filters_post_hoc(
    server: ModuleType, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/search?query=beach&page=1&per_page=8",
        json={
            "page": 1,
            "per_page": 8,
            "photos": [
                _photo(1, 1920, 1080),
                _photo(2, 1000, 1000),
                _photo(3, 3840, 2160),
                _photo(4, 1280, 720),
                _photo(5, 1920, 1080),
            ],
        },
    )
    async with _session(server) as session:
        result = await session.call_tool(
            "pexels_search_photos",
            {"query": "beach", "aspect_ratio": "16:9", "min_width": 1500, "per_page": 2},
        )

    body = _structured(result)
    assert [p["id"] for p in body["photos"]] == [1, 3]
    assert body["per_page"] == 2
    assert body["count"] == 2
    assert body["has_more"] is False
    assert "filter_diagnostics" not in body


async def test_search_photos_reports_diagnostics_when_filter_wipes_page(
    server: ModuleType, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/search?query=beach&page=1&per_page=60",
        json={"page": 1, "per_page": 60, "photos": [_photo(1, 1000, 1000)]},
    )
    async with _session(server) as session:
        result = await session.call_tool(
            "pexels_search_photos",
            {"query": "beach", "aspect_ratio": "16:9", "min_height": 500},
        )

    body = _structured(result)
    assert body["photos"] == []
    assert body["filter_diagnostics"] == {
        "applied_filters": {"min_height": 500, "aspect_ratio": "16:9"},
        "pre_filter_count": 1,
        "post_filter_count": 0,
        "suggestion": (
            "Filters rejected every candidate. Retry without aspect_ratio "
            "(crop to target ratio in post)."
        ),
    }


async def test_min_dimension_diagnostics_suggest_lowering_the_floor(
    server: ModuleType, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/curated?page=1&per_page=60",
        json={"page": 1, "per_page": 60, "photos": [_photo(1, 800, 600)]},
    )
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_curated_photos", {"min_width": 4000})

    body = _structured(result)
    assert body["photos"] == []
    assert body["filter_diagnostics"]["applied_filters"] == {"min_width": 4000}
    assert body["filter_diagnostics"]["suggestion"] == (
        "Filters rejected every candidate. Lower min_width / min_height."
    )


async def test_curated_photos_without_filters(server: ModuleType, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/curated?page=3&per_page=1",
        json={
            "page": 3,
            "per_page": 1,
            "next_page": f"{BASE_URL}/v1/curated?page=4",
            "photos": [_photo(9)],
        },
    )
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_curated_photos", {"page": 3, "per_page": 1})

    body = _structured(result)
    assert [p["id"] for p in body["photos"]] == [9]
    assert body["next_page"] == 4


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        ({"query": ""}, "query"),
        ({"query": "cat", "per_page": 500}, "per_page"),
        ({"query": "cat", "color": "not-a-color"}, "color"),
        ({"query": "cat", "aspect_ratio": "wide"}, "aspect_ratio"),
    ],
)
async def test_search_photos_rejects_invalid_params(
    server: ModuleType, arguments: dict[str, Any], field: str
) -> None:
    async with _session(server) as session:
        result = await session.call_tool("pexels_search_photos", arguments)

    message = _error_text(result)
    assert "Invalid parameters: " in message
    assert field in message


async def test_tool_reports_missing_api_key(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PEXELS_API_KEY")
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_photo", {"photo_id": 1})

    assert "Pexels API key is missing" in _error_text(result)


async def test_http_transport_ignores_env_key(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSPORT", "streamable-http")
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_photo", {"photo_id": 1})

    assert "Pexels API key is missing" in _error_text(result)


async def test_header_key_takes_precedence_over_env(
    server: ModuleType, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/photos/5",
        match_headers={"Authorization": "header-key"},
        json=_photo(5),
    )
    token = pexels_key_ctx.set("header-key")
    try:
        async with _session(server) as session:
            result = await session.call_tool("pexels_get_photo", {"photo_id": 5})
    finally:
        pexels_key_ctx.reset(token)

    assert _structured(result)["photo"]["id"] == 5


async def test_pexels_api_error_is_surfaced_as_tool_error(
    server: ModuleType, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/v1/photos/404", status_code=404, text="Not Found")
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_photo", {"photo_id": 404})

    assert "Pexels API error 404: Not Found" in _error_text(result)


async def test_get_photo(server: ModuleType, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/v1/photos/42", json=_photo(42))
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_photo", {"photo_id": 42})

    body = _structured(result)
    assert body["photo"]["id"] == 42
    assert body["photo"]["image_url"] == "https://images.pexels.com/photos/42/original.jpeg"


async def test_get_photo_rejects_non_positive_id(server: ModuleType) -> None:
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_photo", {"photo_id": 0})

    assert "Invalid parameters: photo_id" in _error_text(result)


async def test_search_videos(server: ModuleType, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=(
            f"{BASE_URL}/v1/videos/search?query=ocean&orientation=portrait&size=medium"
            "&locale=en-US&page=1&per_page=4"
        ),
        json={
            "page": 1,
            "per_page": 4,
            "videos": [_video(1, 1080, 1920), _video(2, 1920, 1080)],
        },
    )
    async with _session(server) as session:
        result = await session.call_tool(
            "pexels_search_videos",
            {
                "query": "ocean",
                "orientation": "portrait",
                "size": "medium",
                "locale": "en-US",
                "min_height": 1500,
                "per_page": 1,
            },
        )

    body = _structured(result)
    assert body["videos"] == [
        {
            "id": 1,
            "page_url": "https://www.pexels.com/video/v-1/",
            "duration_seconds": 12,
            "width": 1080,
            "height": 1920,
            "uploader_name": "Bob",
            "uploader_url": "https://www.pexels.com/@bob",
            "video_url": "https://videos/uhd.mp4",
            "quality": "uhd",
        }
    ]
    assert body["per_page"] == 1


async def test_search_videos_rejects_unknown_locale(server: ModuleType) -> None:
    async with _session(server) as session:
        result = await session.call_tool(
            "pexels_search_videos", {"query": "ocean", "locale": "xx-XX"}
        )

    assert "Invalid parameters: locale" in _error_text(result)


async def test_get_video(server: ModuleType, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/v1/videos/videos/7", json=_video(7))
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_video", {"video_id": 7})

    assert _structured(result)["video"]["video_url"] == "https://videos/uhd.mp4"


async def test_get_video_rejects_non_positive_id(server: ModuleType) -> None:
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_video", {"video_id": -1})

    assert "Invalid parameters: video_id" in _error_text(result)


async def test_get_collection_media(server: ModuleType, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/collections/abc123?type=photos&sort=desc&page=1&per_page=15",
        json={
            "id": "abc123",
            "page": 1,
            "per_page": 15,
            "total_results": 2,
            "media": [{**_photo(1), "type": "Photo"}, {**_video(2), "type": "Video"}],
        },
    )
    async with _session(server) as session:
        result = await session.call_tool(
            "pexels_get_collection_media",
            {"collection_id": "abc123", "type": "photos", "sort": "desc"},
        )

    body = _structured(result)
    assert body["id"] == "abc123"
    assert body["count"] == 2
    assert [p["id"] for p in body["photos"]] == [1]
    assert [v["id"] for v in body["videos"]] == [2]


async def test_get_collection_media_filters_mixed_media(
    server: ModuleType, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/collections/abc123?page=1&per_page=40",
        json={
            "id": "abc123",
            "page": 1,
            "per_page": 40,
            "media": [
                {**_photo(1, 1000, 1000), "type": "Photo"},
                {**_video(2, 1920, 1080), "type": "Video"},
            ],
        },
    )
    async with _session(server) as session:
        result = await session.call_tool(
            "pexels_get_collection_media",
            {"collection_id": "abc123", "aspect_ratio": "16:9", "per_page": 10},
        )

    body = _structured(result)
    assert body["photos"] == []
    assert [v["id"] for v in body["videos"]] == [2]


async def test_get_collection_media_rejects_bad_id(server: ModuleType) -> None:
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_collection_media", {"collection_id": "../etc"})

    assert "Invalid parameters: collection_id" in _error_text(result)


async def test_popular_videos_forwards_native_filters_without_oversampling(
    server: ModuleType, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=(
            f"{BASE_URL}/v1/videos/popular?min_width=1920&min_height=1080"
            "&min_duration=5&max_duration=30&page=1&per_page=15"
        ),
        json={"page": 1, "per_page": 15, "videos": [_video(1), _video(2)]},
    )
    async with _session(server) as session:
        result = await session.call_tool(
            "pexels_get_popular_videos",
            {"min_width": 1920, "min_height": 1080, "min_duration": 5, "max_duration": 30},
        )

    body = _structured(result)
    assert [v["id"] for v in body["videos"]] == [1, 2]
    assert body["per_page"] == 15


async def test_popular_videos_filters_aspect_ratio_post_hoc(
    server: ModuleType, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/videos/popular?min_width=100&page=1&per_page=8",
        json={
            "page": 1,
            "per_page": 8,
            "videos": [_video(1, 1080, 1920), _video(2, 1920, 1080), _video(3, 3840, 2160)],
        },
    )
    async with _session(server) as session:
        result = await session.call_tool(
            "pexels_get_popular_videos",
            {"min_width": 100, "aspect_ratio": "16:9", "per_page": 2},
        )

    body = _structured(result)
    assert [v["id"] for v in body["videos"]] == [2, 3]
    assert body["per_page"] == 2
    assert "filter_diagnostics" not in body


async def test_popular_videos_reports_diagnostics_when_aspect_ratio_wipes_page(
    server: ModuleType, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/videos/popular?page=1&per_page=60",
        json={"page": 1, "per_page": 60, "videos": [_video(1, 1080, 1920)]},
    )
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_popular_videos", {"aspect_ratio": "1:1"})

    body = _structured(result)
    assert body["videos"] == []
    assert body["filter_diagnostics"] == {
        "applied_filters": {"aspect_ratio": "1:1"},
        "pre_filter_count": 1,
        "post_filter_count": 0,
        "suggestion": (
            "Filters rejected every candidate. Retry without aspect_ratio "
            "(crop to target ratio in post)."
        ),
    }


async def test_popular_videos_rejects_inverted_duration_range(server: ModuleType) -> None:
    async with _session(server) as session:
        result = await session.call_tool(
            "pexels_get_popular_videos", {"min_duration": 60, "max_duration": 10}
        )

    assert "min_duration must not exceed max_duration" in _error_text(result)


async def test_featured_collections(server: ModuleType, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/collections/featured?page=1&per_page=2",
        json={
            "page": 1,
            "per_page": 2,
            "total_results": 10,
            "next_page": f"{BASE_URL}/v1/collections/featured?page=2",
            "collections": [
                {
                    "id": "abc123",
                    "title": "Nature",
                    "description": "Green things",
                    "private": False,
                    "media_count": 5,
                    "photos_count": 4,
                    "videos_count": 1,
                }
            ],
        },
    )
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_featured_collections", {"per_page": 2})

    body = _structured(result)
    assert body["collections"] == [
        {
            "id": "abc123",
            "title": "Nature",
            "description": "Green things",
            "private": False,
            "media_count": 5,
            "photos_count": 4,
            "videos_count": 1,
        }
    ]
    assert body["has_more"] is True


async def test_featured_collections_rejects_zero_page(server: ModuleType) -> None:
    async with _session(server) as session:
        result = await session.call_tool("pexels_get_featured_collections", {"page": 0})

    assert "Invalid parameters: page" in _error_text(result)


async def test_lists_resource_templates(server: ModuleType) -> None:
    async with _session(server) as session:
        templates = (await session.list_resource_templates()).resourceTemplates

    assert sorted(t.uriTemplate for t in templates) == [
        "pexels://collection/{collection_id}",
        "pexels://photo/{photo_id}",
        "pexels://video/{video_id}",
    ]


async def test_photo_resource(server: ModuleType, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/v1/photos/42", json=_photo(42))
    async with _session(server) as session:
        result = await session.read_resource(AnyUrl("pexels://photo/42"))

    assert _resource_json(result.contents)["photo"]["id"] == 42


async def test_video_resource(server: ModuleType, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=f"{BASE_URL}/v1/videos/videos/7", json=_video(7))
    async with _session(server) as session:
        result = await session.read_resource(AnyUrl("pexels://video/7"))

    assert _resource_json(result.contents)["video"]["id"] == 7


async def test_collection_resource(server: ModuleType, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{BASE_URL}/v1/collections/abc123?page=1&per_page=15",
        json={"id": "abc123", "page": 1, "per_page": 15, "media": [{**_photo(1), "type": "Photo"}]},
    )
    async with _session(server) as session:
        result = await session.read_resource(AnyUrl("pexels://collection/abc123"))

    body = _resource_json(result.contents)
    assert body["id"] == "abc123"
    assert [p["id"] for p in body["photos"]] == [1]


@pytest.mark.parametrize(
    "uri",
    [
        "pexels://photo/not-a-number",
        "pexels://video/0",
        "pexels://collection/bad.id",
    ],
)
async def test_resources_reject_invalid_ids(server: ModuleType, uri: str) -> None:
    async with _session(server) as session:
        with pytest.raises(McpError):
            await session.read_resource(AnyUrl(uri))


async def test_find_hero_image_prompt(server: ModuleType) -> None:
    async with _session(server) as session:
        result = await session.get_prompt(
            "find_hero_image", {"topic": "coffee shop", "brand_color": "orange"}
        )

    content = result.messages[0].content
    assert isinstance(content, TextContent)
    assert content.text == (
        "Find a stock photo on Pexels for: coffee shop.\n"
        "Call `pexels_search_photos` with orientation='landscape', "
        "aspect_ratio='16:9', color='orange', min_width=1920.\n"
        "Return the best `image_url` as a Markdown link with the "
        "mandatory `photographer` credit."
    )


@pytest.mark.parametrize(
    ("resolution", "size", "min_width"),
    [("4K", "large", 3840), ("1080p", "medium", 1920)],
)
async def test_find_broll_prompt(
    server: ModuleType, resolution: str, size: str, min_width: int
) -> None:
    async with _session(server) as session:
        result = await session.get_prompt(
            "find_broll", {"topic": "city at night", "resolution": resolution}
        )

    content = result.messages[0].content
    assert isinstance(content, TextContent)
    assert f"size='{size}'" in content.text
    assert f"min_width={min_width}" in content.text
    assert "pexels_search_videos" in content.text


def test_build_oauth_settings_is_disabled_in_stdio(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSPORT", "stdio")
    assert server._build_oauth_settings() is None


def test_build_oauth_settings_requires_server_url(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSPORT", "streamable-http")
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)
    with pytest.raises(RuntimeError, match="MCP_SERVER_URL"):
        server._build_oauth_settings()


def test_build_oauth_settings_wires_provider_and_scopes(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_SERVER_URL", "https://pexels.example.com")
    monkeypatch.delenv("REDIS_URL", raising=False)

    built = server._build_oauth_settings()

    assert built is not None
    _provider, auth = built
    assert str(auth.issuer_url) == "https://pexels.example.com/"
    assert str(auth.resource_server_url) == "https://pexels.example.com/"
    assert auth.required_scopes == [server.MCP_SCOPE]
    assert auth.client_registration_options is not None
    assert auth.client_registration_options.enabled is True


def test_transport_security_uses_explicit_allowlist(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "a.example.com, b.example.com,")

    settings = server._build_transport_security()

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["a.example.com", "b.example.com"]


def test_transport_security_derives_allowlist_from_server_url(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("MCP_SERVER_URL", "https://pexels.example.com")

    settings = server._build_transport_security()

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["pexels.example.com", "pexels.example.com:*"]


def test_transport_security_is_off_without_host_config(
    server: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)

    assert server._build_transport_security().enable_dns_rebinding_protection is False
