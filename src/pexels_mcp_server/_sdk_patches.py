"""Upstream-bound patches against the ``mcp`` Python SDK.

These patches address two issues with ``mcp 1.27.1`` that bleed tokens
into every tool call. Both should eventually be fixed upstream — this
module is the only place in the codebase allowed to mutate third-party
state, and the only contract is that ``apply()`` is idempotent.

Issue 1 — ``model_dump`` does not pass ``exclude_unset=True``
-----------------------------------------------------------

``FuncMetadata.convert_result`` validates the tool's return value
against the auto-built Pydantic model, then dumps it via
``model_dump(mode="json", by_alias=True)``. ``_create_model_from_typeddict``
assigns ``default=None`` to every optional TypedDict field, so the dump
emits ``"field": null`` for keys the tool never set. The auto-generated
``outputSchema`` is strict (non-nullable ``int`` / non-nullable nested
TypedDict), so ``jsonschema.validate`` rejects every call with
``"None is not of type 'object'"``.

The SDK acknowledges the missing flag itself in a comment at
``_create_model_from_typeddict``:

    # The model should use exclude_unset=True when dumping to get
    # TypedDict semantics

We supply that flag here.

Issue 2 — every tool result is duplicated as indented JSON
----------------------------------------------------------

The SDK's ``_convert_to_content`` serialises the dict to a ``TextContent``
via ``pydantic_core.to_json(result, indent=2)``. The same payload is
then **also** placed in ``structuredContent`` (compact). For a 15-photo
search result that means **~7 KB of indented JSON shipped on top of
the ~6 KB structured payload — every single tool call**. Five calls in
one conversation burn ~8 800 redundant tokens of the user's quota
before the agent even composes its reply.

MCP spec 2025-11-25 reads ``structuredContent`` as the canonical machine-
readable payload (SEP-1303). Clients that need the human-friendly text
get a one-line marker pointing at it. The full JSON stays available
through ``structuredContent``; nothing is dropped, only the duplication.

If we ever ship to a client that does **not** read ``structuredContent``,
flip ``_DROP_DUPLICATE_TEXT_CONTENT`` to ``False`` and the original
``indent=2`` text content comes back.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp.utilities import func_metadata as _fm
from mcp.types import CallToolResult, TextContent

# When True (default): tools that return a structured dict ship an empty
# content list — the structuredContent is the canonical payload.
# When False: ship the legacy SDK behaviour (indented JSON in content).
_DROP_DUPLICATE_TEXT_CONTENT = True


def _patched_convert_result(self: _fm.FuncMetadata, result: Any) -> Any:
    """Drop-in replacement for ``FuncMetadata.convert_result``.

    See module docstring for the two issues this addresses.
    """
    if isinstance(result, CallToolResult):
        if self.output_schema is not None:
            assert self.output_model is not None
            self.output_model.model_validate(result.structuredContent)
        return result

    if self.output_schema is None:
        # Tool with no declared output schema: keep the upstream behaviour
        # (free-form text / image / etc.).
        return _fm._convert_to_content(result)

    if self.wrap_output:
        result = {"result": result}

    assert self.output_model is not None
    validated = self.output_model.model_validate(result)
    structured_content = validated.model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
    )

    if _DROP_DUPLICATE_TEXT_CONTENT:
        # MCP spec 2025-11-25: structuredContent is the canonical machine-
        # readable payload. We ship a one-line marker in ``content`` so
        # any client that reads ``content`` knows where to look, without
        # paying the cost of a duplicate indented dump of the entire
        # payload. Saves ~50% of every tool call's bandwidth.
        marker = TextContent(
            type="text",
            text="See structuredContent for the result payload.",
        )
        return ([marker], structured_content)

    return (_fm._convert_to_content(result), structured_content)


def apply() -> None:
    """Install every patch declared in this module.

    Idempotent — safe to call from both ``__init__.py`` (so any consumer
    importing the package gets the patches) and explicit test setup.
    """
    _fm.FuncMetadata.convert_result = _patched_convert_result  # type: ignore[method-assign]
