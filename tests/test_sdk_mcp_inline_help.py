# tests/test_sdk_mcp_inline_help.py
# Tests for the pocketpaw_widgets in-process MCP tool handlers.
#
# Changes:
#   - 2026-05-31 (fix/bridge-start-flow-to-chat, RFC 13): added
#     TestStartFlowHandler. The start_flow authoring tool was bridged onto
#     this core server so the cloud chat agent can reach it; these tests
#     prove the MCP handler scaffolds a real {version, ui} flow tree (reusing
#     the same builder as the runtime StartFlowTool) and returns an
#     agent-readable error for a bad descriptor.

import pytest


@pytest.mark.asyncio
async def test_inline_widget_help_handler_returns_payload_for_chart():
    from pocketpaw.agents.sdk_mcp_widgets import _get_inline_widget_help_handler

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
    from pocketpaw.agents.sdk_mcp_widgets import _get_inline_widget_help_handler
    from pocketpaw.ripple._design import RIPPLE_DESIGN_RULES

    out = await _get_inline_widget_help_handler({})
    text_block = next(
        (c for c in out.get("content", []) if c.get("type") == "text"),
        None,
    )
    assert text_block is not None
    assert text_block["text"] == RIPPLE_DESIGN_RULES, (
        "no-types call must return the full RIPPLE_DESIGN_RULES verbatim"
    )


def _text_of(result: dict) -> str:
    block = next((c for c in result.get("content", []) if c.get("type") == "text"), None)
    assert block is not None, "handler must return a text content block"
    return block["text"]


class TestStartFlowHandler:
    """The bridged ``start_flow`` MCP handler — the reachability fix for
    RFC 13 M3. It must scaffold a real {version, ui} flow tree from a tiny
    descriptor, using the same builder as the runtime ``StartFlowTool``."""

    @pytest.mark.asyncio
    async def test_scaffolds_full_flow_doc(self):
        import json

        from pocketpaw.agents.sdk_mcp_widgets import _start_flow_handler

        out = await _start_flow_handler({"flow_type": "onboarding_wizard"})
        assert not out.get("is_error"), "a valid flow_type must not error"
        doc = json.loads(_text_of(out))
        # The descriptor expanded into a full {version, ui} flow doc — NOT a
        # flat single spec. A nested chain/chain_map step tree is the whole
        # point of start_flow (the anti-pattern it prevents is a flat spec).
        assert set(doc) >= {"version", "ui"}
        assert isinstance(doc["ui"], dict)
        assert "chain" in doc["ui"] or "chain_map" in doc["ui"], (
            "start_flow must return a multi-step chain/chain_map tree, not a flat spec"
        )

    @pytest.mark.asyncio
    async def test_handler_matches_runtime_tool_builder(self):
        """The MCP handler and the runtime StartFlowTool share the builder,
        so identical descriptors must yield identical trees — the cloud
        agent and the runtime registry scaffold the same flow."""
        import json

        from pocketpaw.agents.sdk_mcp_widgets import _start_flow_handler
        from pocketpaw.tools.builtin.flow_tool import StartFlowTool

        mcp_doc = json.loads(
            _text_of(await _start_flow_handler({"flow_type": "onboarding_wizard"}))
        )
        runtime_raw = await StartFlowTool().execute(flow_type="onboarding_wizard")
        runtime_doc = json.loads(runtime_raw)
        assert mcp_doc == runtime_doc

    @pytest.mark.asyncio
    async def test_unknown_flow_type_returns_agent_readable_error(self):
        from pocketpaw.agents.sdk_mcp_widgets import _start_flow_handler

        out = await _start_flow_handler({"flow_type": "definitely_not_a_template"})
        assert out.get("is_error") is True
        # The error names the valid templates so the model can retry.
        assert "onboarding_wizard" in _text_of(out)

    @pytest.mark.asyncio
    async def test_missing_flow_type_returns_error(self):
        from pocketpaw.agents.sdk_mcp_widgets import _start_flow_handler

        out = await _start_flow_handler({})
        assert out.get("is_error") is True
        assert "flow_type" in _text_of(out)

    @pytest.mark.asyncio
    async def test_config_as_json_string_is_coerced(self):
        """Some callers pass ``config`` as a JSON string; the handler must
        coerce it rather than reject it (parity with StartFlowTool)."""
        import json

        from pocketpaw.agents.sdk_mcp_widgets import _start_flow_handler

        out = await _start_flow_handler(
            {"flow_type": "onboarding_wizard", "config": '{"product_name": "Acme"}'}
        )
        assert not out.get("is_error")
        # Valid {version, ui} doc still produced with the override applied.
        doc = json.loads(_text_of(out))
        assert set(doc) >= {"version", "ui"}
