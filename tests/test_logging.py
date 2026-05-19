"""Tests for the structured-JSON logger, LOG_FORMAT resolution, and the
HTTPS / MCP_SERVER_URL guard that gates HTTP-mode boot."""

from __future__ import annotations

import json
import logging
import os
from unittest import mock

import pytest

from pexels_mcp_server.__main__ import (
    _JsonFormatter,
    _resolve_log_format,
    _validate_http_env,
)


def test_json_formatter_emits_valid_json_with_expected_fields() -> None:
    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name="pexels_mcp_server.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    line = formatter.format(record)
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "pexels_mcp_server.test"
    assert payload["msg"] == "hello world"
    assert "ts" in payload
    assert payload["ts"].endswith("+00:00")


def test_json_formatter_serializes_exception() -> None:
    formatter = _JsonFormatter()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            name="x",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="oops",
            args=None,
            exc_info=sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert "RuntimeError: boom" in payload["exc"]


def test_resolve_log_format_defaults_to_text_for_stdio() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _resolve_log_format("stdio") == "text"


def test_resolve_log_format_defaults_to_json_for_http() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _resolve_log_format("streamable-http") == "json"


def test_resolve_log_format_honors_explicit_override() -> None:
    with mock.patch.dict(os.environ, {"LOG_FORMAT": "text"}, clear=True):
        assert _resolve_log_format("streamable-http") == "text"
    with mock.patch.dict(os.environ, {"LOG_FORMAT": "json"}, clear=True):
        assert _resolve_log_format("stdio") == "json"


def test_resolve_log_format_ignores_unknown_value() -> None:
    with mock.patch.dict(os.environ, {"LOG_FORMAT": "yaml"}, clear=True):
        # Falls back to transport-driven default.
        assert _resolve_log_format("streamable-http") == "json"
        assert _resolve_log_format("stdio") == "text"


# --- HTTPS / MCP_SERVER_URL guard ---------------------------------------


def test_validate_http_env_rejects_missing_url() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(SystemExit) as excinfo:
            _validate_http_env()
        assert excinfo.value.code == 2


def test_validate_http_env_rejects_plain_http_in_prod() -> None:
    """MCP spec 2025-06-18 §Communication Security demands HTTPS."""
    with mock.patch.dict(
        os.environ, {"MCP_SERVER_URL": "http://pexels-mcp.example.com"}, clear=True
    ):
        with pytest.raises(SystemExit) as excinfo:
            _validate_http_env()
        assert excinfo.value.code == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://pexels-mcp.example.com",
        "https://pexels-mcp.example.com:8443",
        "https://sub.example.org",
    ],
)
def test_validate_http_env_accepts_https(url: str) -> None:
    with mock.patch.dict(os.environ, {"MCP_SERVER_URL": url}, clear=True):
        _validate_http_env()  # must not raise


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://localhost",
        "http://[::1]:8000",
    ],
)
def test_validate_http_env_accepts_http_loopback_for_dev(url: str) -> None:
    """Plain http is allowed for loopback only, so local dev still works."""
    with mock.patch.dict(os.environ, {"MCP_SERVER_URL": url}, clear=True):
        _validate_http_env()  # must not raise


def test_validate_http_env_rejects_non_http_schemes() -> None:
    """A 'file://' or 'javascript:' URL must never be accepted."""
    with mock.patch.dict(os.environ, {"MCP_SERVER_URL": "file:///etc/passwd"}, clear=True):
        with pytest.raises(SystemExit) as excinfo:
            _validate_http_env()
        assert excinfo.value.code == 2
