"""Tests for the get_inline_widget_help MCP tool handler."""

import pytest


@pytest.mark.asyncio
async def test_inline_widget_help_handler_returns_payload_for_chart():
    from pocketpaw.agents.sdk_mcp_pocket import _get_inline_widget_help_handler

    out = await _get_inline_widget_help_handler({"types": ["chart"]})
    assert isinstance(out, dict)
    # Handler returns {"content": [{"type": "text", "text": "<plain string>"}]}
    text_block = next(
        (c for c in out.get("content", []) if c.get("type") == "text"),
        None,
    )
    assert text_block is not None
    body = text_block["text"]
    assert "chart" in body.lower()


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
    # The body is the raw RIPPLE_DESIGN_RULES string — verify by checking
    # the first non-empty line.
    first_heading = RIPPLE_DESIGN_RULES.split("\n", 1)[0].strip()
    assert first_heading in text_block["text"], (
        f"expected first heading {first_heading!r} in handler response"
    )
