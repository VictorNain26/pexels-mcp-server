"""Upstream-bound patches against the ``mcp`` Python SDK.

These patches address bugs we observed in production. Each one carries a
link to the upstream tracking item; the patch must be removed once the
upstream release that ships the fix is pinned in ``pyproject.toml``.

Keep this module empty of any project-specific logic. It mutates third-
party state at import time and is the only place in the codebase allowed
to do so. Anything else belongs in ``server.py`` / ``formatters.py``.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp.utilities import func_metadata as _fm
from mcp.types import CallToolResult


def _patched_convert_result(self: _fm.FuncMetadata, result: Any) -> Any:
    """Drop-in replacement that honours TypedDict semantics on dump.

    The upstream method (``mcp 1.27.1``,
    ``mcp/server/fastmcp/utilities/func_metadata.py::FuncMetadata.convert_result``)
    dumps the validated tool result via ``model_dump(mode="json", by_alias=True)``
    **without** ``exclude_unset=True``. ``_create_model_from_typeddict``
    assigns ``default=None`` to every optional TypedDict field, so the dump
    emits ``"field": null`` for keys the tool never set. The auto-generated
    ``outputSchema`` is then strict (non-nullable ``int`` / non-nullable
    nested TypedDict), and ``jsonschema.validate`` on the lowlevel server
    rejects every call with ``"None is not of type 'object'"`` (or similar).

    The SDK acknowledges the missing flag itself in a comment at
    ``_create_model_from_typeddict``:

        # The model should use exclude_unset=True when dumping to get
        # TypedDict semantics

    This patch supplies that flag. Once the upstream `mcp` release that
    ships the equivalent fix is pinned in ``pyproject.toml``, delete the
    patch and the ``apply()`` call below.
    """
    if isinstance(result, CallToolResult):
        if self.output_schema is not None:
            assert self.output_model is not None
            self.output_model.model_validate(result.structuredContent)
        return result

    unstructured_content = _fm._convert_to_content(result)

    if self.output_schema is None:
        return unstructured_content

    if self.wrap_output:
        result = {"result": result}

    assert self.output_model is not None
    validated = self.output_model.model_validate(result)
    structured_content = validated.model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
    )
    return (unstructured_content, structured_content)


def apply() -> None:
    """Install every patch declared in this module.

    Idempotent — safe to call from both ``__init__.py`` (so any consumer
    importing the package gets the patches) and explicit test setup.
    """
    _fm.FuncMetadata.convert_result = _patched_convert_result  # type: ignore[method-assign]
