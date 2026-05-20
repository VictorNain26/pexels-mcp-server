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

Issue 2 — text content is serialised with ``indent=2``
-------------------------------------------------------

The SDK's ``_convert_to_content`` serialises the tool result dict to a
``TextContent`` via ``pydantic_core.to_json(result, indent=2)``. That is
roughly **+30 % bytes** versus a compact JSON dump (for a 15-photo
search: ~7 100 c indented vs ~5 400 c compact), and the indented blob
ships *on top of* the same payload re-encoded in ``structuredContent``.

Why we don't drop the text content entirely
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

MCP spec 2025-11-25 designates ``structuredContent`` as the canonical
machine-readable payload (SEP-1303). In principle a marker like
``"See structuredContent for the result payload."`` is enough. **In
practice (May 2026), claude.ai's custom MCP connector path still feeds
only ``content`` to the model** — a marker-only response causes the
agent to fall back on hallucinated CDN patterns instead of reading the
URLs that sat in ``structuredContent`` the whole time.

So this patch ships the payload in *both* fields, but uses **compact
JSON** in ``content`` (no indent, no whitespace) instead of the SDK
default. We get a ~30 % saving on the text-content side without
betting on client implementation details we don't control.

When a future Claude client confirms it consumes ``structuredContent``
natively, drop the duplicate text content by setting the compact JSON
to a short marker again — but treat that change as user-visible and
test it end-to-end first.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp.utilities import func_metadata as _fm
from mcp.types import CallToolResult, TextContent


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

    # Compact JSON for the human-readable channel. Same payload as
    # structuredContent, but ~30 % smaller than the SDK's ``indent=2``
    # default. The agent reads this in claude.ai today; structuredContent
    # is there for any client that prefers the typed path.
    compact_text = json.dumps(structured_content, separators=(",", ":"), ensure_ascii=False)
    return ([TextContent(type="text", text=compact_text)], structured_content)


def apply() -> None:
    """Install every patch declared in this module.

    Idempotent — safe to call from both ``__init__.py`` (so any consumer
    importing the package gets the patches) and explicit test setup.
    """
    _fm.FuncMetadata.convert_result = _patched_convert_result  # type: ignore[method-assign]
