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
#   - 2026-06-15 (feat/chain-flow-v2 — SPLIT-BRAIN FIX): added flat-graph
#     coverage. The SDK handler was preset-only while the prompt told the model
#     to author a flat `steps` graph, so flat authoring was rejected at the tool
#     boundary (the #1 v2 flakiness cause). New tests prove the handler now
#     routes a flat `steps` descriptor through `build_flow_from_descriptor`
#     (returning a doc that matches the runtime StartFlowTool byte-for-byte),
#     coerces a `steps` JSON string, repairs a dead-end last select, and surfaces
#     a FlowBuildError verbatim for a genuine structural bug.

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

    # ----- CHAIN FLOW v2 flat-graph routing (the split-brain fix) ----------

    @pytest.mark.asyncio
    async def test_accepts_flat_steps_graph_and_returns_doc(self):
        """THE split-brain fix: a FLAT `steps` descriptor (what the prompt tells
        the model to author) routes through the general builder and returns a
        {version, ui} doc — NOT the old 'needs a flow_type' error."""
        import json

        from pocketpaw.agents.sdk_mcp_widgets import _start_flow_handler

        out = await _start_flow_handler(
            {
                "flow": "x",
                "entry": "pick",
                "title": "Demo",
                "steps": [
                    {
                        "id": "pick",
                        "kind": "select",
                        "title": "Pick one",
                        "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                        "branch": {"a": "done", "b": "done"},
                    },
                    {
                        "id": "done",
                        "kind": "confirm",
                        "title": "Done",
                        "review": [{"label": "Choice", "value": "{pick.label}"}],
                        "complete": {"action": "chat", "message": "All set."},
                    },
                ],
            }
        )
        assert not out.get("is_error"), "a flat steps graph must NOT error"
        doc = json.loads(_text_of(out))
        assert set(doc) >= {"version", "ui"}
        assert "chain_map" in doc["ui"], "the branching select must materialize a chain_map"

    @pytest.mark.asyncio
    async def test_flat_graph_matches_runtime_tool_builder(self):
        """The handler's flat-graph path mirrors StartFlowTool.execute, so an
        identical flat descriptor yields the identical doc through both."""
        import json

        from pocketpaw.agents.sdk_mcp_widgets import _start_flow_handler
        from pocketpaw.tools.builtin.flow_tool import StartFlowTool

        descriptor = {
            "flow": "demo",
            "entry": "f",
            "steps": [
                {
                    "id": "f",
                    "kind": "form",
                    "title": "F",
                    "fields": [{"id": "a", "label": "A", "type": "text", "required": True}],
                    "next": "end",
                },
                {
                    "id": "end",
                    "kind": "confirm",
                    "title": "End",
                    "review": [{"label": "A", "value": "{f.a}"}],
                    "complete": {"action": "chat", "message": "ok"},
                },
            ],
        }
        mcp_doc = json.loads(
            _text_of(
                await _start_flow_handler(
                    {
                        "flow": descriptor["flow"],
                        "entry": descriptor["entry"],
                        "steps": descriptor["steps"],
                    }
                )
            )
        )
        runtime_doc = json.loads(
            await StartFlowTool().execute(
                flow=descriptor["flow"], entry=descriptor["entry"], steps=descriptor["steps"]
            )
        )
        assert mcp_doc == runtime_doc

    @pytest.mark.asyncio
    async def test_steps_as_json_string_is_coerced(self):
        """SDK callers may pass `steps` as a JSON string through the flat
        signature — the handler coerces it (parity with StartFlowTool)."""
        import json

        from pocketpaw.agents.sdk_mcp_widgets import _start_flow_handler

        steps = [
            {
                "id": "only",
                "kind": "confirm",
                "title": "Done",
                "complete": {"action": "chat", "message": "ok"},
            }
        ]
        out = await _start_flow_handler({"flow": "x", "entry": "only", "steps": json.dumps(steps)})
        assert not out.get("is_error")
        doc = json.loads(_text_of(out))
        assert set(doc) >= {"version", "ui"}

    @pytest.mark.asyncio
    async def test_repairs_dead_end_last_select_through_handler(self):
        """Genesis forgiveness reaches the SDK boundary: a dead-end LAST select
        is repaired into a terminal step, so the handler returns a doc, not an
        error."""
        import json

        from pocketpaw.agents.sdk_mcp_widgets import _start_flow_handler

        out = await _start_flow_handler(
            {
                "flow": "ok",
                "entry": "pick",
                "steps": [
                    {
                        "id": "pick",
                        "kind": "select",
                        "title": "Pick",
                        "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                    }
                ],
            }
        )
        assert not out.get("is_error")
        doc = json.loads(_text_of(out))
        assert doc["ui"]["onComplete"]["kind"] == "chat"

    @pytest.mark.asyncio
    async def test_flat_graph_real_bug_surfaces_error(self):
        """A genuine structural bug (dangling branch target) is surfaced verbatim
        so the model can fix the flat graph and retry."""
        from pocketpaw.agents.sdk_mcp_widgets import _start_flow_handler

        out = await _start_flow_handler(
            {
                "flow": "bad",
                "entry": "pick",
                "steps": [
                    {
                        "id": "pick",
                        "kind": "select",
                        "title": "Pick",
                        "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                        "branch": {"a": "ghost", "b": "ghost"},
                    }
                ],
            }
        )
        assert out.get("is_error") is True
        assert "ghost" in _text_of(out)
