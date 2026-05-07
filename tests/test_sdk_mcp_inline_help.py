"""Tests for the get_inline_widget_help MCP tool handler."""

import pytest


@pytest.mark.asyncio
async def test_inline_widget_help_handler_returns_payload_for_chart():
    from pocketpaw.agents.sdk_mcp_pocket import _get_inline_widget_help_handler

    out = await _get_inline_widget_help_handler({"types": ["chart"]})
    assert isinstance(out, dict)
    text_block = next(
        (c for c in out.get("content", []) if c.get("type") == "text"),
        None,
    )
    assert text_block is not None
    body = text_block["text"]
    assert "chart" in body.lower()
    # Chart-specific content must be present, not just the word "chart" —
    # confirms the filter actually returned chart schema rather than an
    # arbitrary fallback.
    assert any(kind in body.lower() for kind in ("bar", "line", "pie")), (
        "chart-specific schema detail must appear when chart is requested"
    )


@pytest.mark.asyncio
async def test_inline_widget_help_handler_no_types_returns_full_catalog():
    from ee.ripple._design import RIPPLE_DESIGN_RULES
    from pocketpaw.agents.sdk_mcp_pocket import _get_inline_widget_help_handler

    out = await _get_inline_widget_help_handler({})
    text_block = next(
        (c for c in out.get("content", []) if c.get("type") == "text"),
        None,
    )
    assert text_block is not None
    assert text_block["text"] == RIPPLE_DESIGN_RULES, (
        "no-types call must return the full RIPPLE_DESIGN_RULES verbatim"
    )
