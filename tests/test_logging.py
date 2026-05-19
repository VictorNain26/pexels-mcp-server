"""Tests for the structured-JSON logger and LOG_FORMAT resolution."""

from __future__ import annotations

import json
import logging
import os
from unittest import mock

from pexels_mcp_server.__main__ import _JsonFormatter, _resolve_log_format


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
    assert "ts" in payload and payload["ts"].endswith("+00:00")


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
