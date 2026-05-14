"""Smoke tests for the edit specialist surface.

Covers the public wiring:
  * mcp_tool exports EDIT_TOOL_ID and registers a sibling MCP tool
  * runtime imports + input/output models work as advertised
  * tool factories produce StructuredTool objects with the right names
  * main-agent interaction prompt is the thin delegation variant;
    specialist prompt is the heavy variant
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestEditMCPTool:
    def test_edit_tool_id_in_specialist_tool_ids(self) -> None:
        from ee.agent.pocket_specialist.mcp_tool import (
            EDIT_TOOL_ID,
            POCKET_SPECIALIST_TOOL_IDS,
        )

        assert EDIT_TOOL_ID == "mcp__pocketpaw_pocket_specialist__edit"
        assert EDIT_TOOL_ID in POCKET_SPECIALIST_TOOL_IDS

    def test_create_and_edit_both_registered(self) -> None:
        from ee.agent.pocket_specialist.mcp_tool import (
            CREATE_TOOL_ID,
            EDIT_TOOL_ID,
            POCKET_SPECIALIST_TOOL_IDS,
        )

        assert CREATE_TOOL_ID in POCKET_SPECIALIST_TOOL_IDS
        assert EDIT_TOOL_ID in POCKET_SPECIALIST_TOOL_IDS
        assert len(POCKET_SPECIALIST_TOOL_IDS) == 2


class TestEditInputOutput:
    def test_input_validates_pocket_id_and_intent(self) -> None:
        from ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

        ok = PocketSpecialistEditInput(pocket_id="p1", intent="rename row 1")
        assert ok.pocket_id == "p1"
        assert ok.intent == "rename row 1"

    def test_input_rejects_blank_intent(self) -> None:
        from pydantic import ValidationError

        from ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

        with pytest.raises(ValidationError):
            PocketSpecialistEditInput(pocket_id="p1", intent="hi")

    def test_input_rejects_empty_pocket_id(self) -> None:
        from pydantic import ValidationError

        from ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

        with pytest.raises(ValidationError):
            PocketSpecialistEditInput(pocket_id="", intent="add a button")

    def test_output_shape(self) -> None:
        from ee.agent.pocket_specialist.runtime import PocketSpecialistEditOutput

        out = PocketSpecialistEditOutput(
            ok=True,
            pocket_id="p1",
            ops=[{"op": "set_state", "args": {"path": "filter", "value": "done"}}],
            duration_ms=300,
            backend_used="langchain_react",
        )
        assert out.ok is True
        assert len(out.ops) == 1
        assert out.ops[0]["op"] == "set_state"


class TestEditToolFactories:
    def test_make_edit_pocket_tools_returns_expected_tool_names(self) -> None:
        from ee.agent.pocket_specialist.tools import make_edit_pocket_tools

        tools = make_edit_pocket_tools(pocket_id="p1")
        names = [t.name for t in tools]
        # Granular ops the edit specialist gets. NO get_pocket — the
        # parent agent fetches the pocket and passes it in the user
        # message; the specialist has no read tool by design.
        for expected in (
            "set_state",
            "append_state",
            "remove_state",
            "patch_state",
            "set_node_prop",
            "add_node",
            "replace_node",
            "move_node",
            "remove_node",
        ):
            assert expected in names, f"missing tool: {expected}"
        # Explicit absence — adding get_pocket back here is a regression.
        assert "get_pocket" not in names, (
            "edit specialist must not have a read tool; parent agent passes "
            "the pocket payload in the user message instead"
        )

    def test_pocket_id_is_closed_over_not_exposed_to_llm(self) -> None:
        """The LLM should NEVER see pocket_id as an argument — it's
        baked into the closure. Verify by checking the args_schema."""
        from ee.agent.pocket_specialist.tools import make_set_state_tool

        tool = make_set_state_tool(pocket_id="p1")
        schema_fields = tool.args_schema.model_fields  # type: ignore[union-attr]
        assert "pocket_id" not in schema_fields
        # set_state's real LLM-facing args:
        assert "path" in schema_fields
        assert "value" in schema_fields

    def test_capture_records_op_invocations(self) -> None:
        """The capture side-channel must record each op call so the
        runtime can return them to the main agent."""
        import asyncio

        from ee.agent.pocket_specialist import tools as tools_mod
        from ee.agent.pocket_specialist.tools import make_set_state_tool

        capture: dict = {}
        with patch.object(
            tools_mod,
            "_capture_op",
            wraps=tools_mod._capture_op,
        ) as wrapped:
            tool = make_set_state_tool(pocket_id="p1", capture=capture)
            with patch(
                "ee.cloud.pockets.agent_context.set_state_for_agent",
                new=AsyncMock(return_value={"ok": True}),
            ):
                asyncio.run(tool.coroutine(path="filter", value="done"))
        wrapped.assert_called_once()
        assert capture.get("ops") == [
            {"op": "set_state", "args": {"path": "filter", "value": "done"}}
        ]


class TestPropArrayItemToolFactories:
    async def test_set_prop_array_item_tool_invokes_wrapper(self) -> None:
        from ee.agent.pocket_specialist import tools

        capture: dict = {}
        tool = tools.make_set_prop_array_item_tool(pocket_id="p1", capture=capture)
        with patch(
            "ee.cloud.pockets.agent_context.set_prop_array_item_for_agent",
            new_callable=AsyncMock,
        ) as wrapper:
            wrapper.return_value = {
                "ok": True,
                "item_index": 0,
                "item": {"x": 1},
                "old_item": {"x": 0},
            }
            result = await tool.coroutine(
                node_id="n_chart000",
                prop="data",
                match={"index": 0},
                partial={"x": 1},
            )
        wrapper.assert_awaited_once_with("p1", "n_chart000", "data", {"index": 0}, {"x": 1})
        assert result["ok"] is True
        assert capture["ops"] == [
            {
                "op": "set_prop_array_item",
                "args": {"node_id": "n_chart000", "prop": "data", "match": {"index": 0}},
            }
        ]

    async def test_append_prop_array_item_tool_invokes_wrapper(self) -> None:
        from ee.agent.pocket_specialist import tools

        capture: dict = {}
        tool = tools.make_append_prop_array_item_tool(pocket_id="p1", capture=capture)
        with patch(
            "ee.cloud.pockets.agent_context.append_prop_array_item_for_agent",
            new_callable=AsyncMock,
        ) as wrapper:
            wrapper.return_value = {"ok": True, "item_index": 3, "item": {"x": 9}}
            result = await tool.coroutine(
                node_id="n_chart000",
                prop="data",
                value={"x": 9},
                after={"id": "row_b"},
            )
        wrapper.assert_awaited_once_with("p1", "n_chart000", "data", {"x": 9}, {"id": "row_b"})
        assert result["ok"] is True
        assert capture["ops"] == [
            {
                "op": "append_prop_array_item",
                "args": {"node_id": "n_chart000", "prop": "data", "after": {"id": "row_b"}},
            }
        ]

    async def test_remove_prop_array_item_tool_invokes_wrapper(self) -> None:
        from ee.agent.pocket_specialist import tools

        capture: dict = {}
        tool = tools.make_remove_prop_array_item_tool(pocket_id="p1", capture=capture)
        with patch(
            "ee.cloud.pockets.agent_context.remove_prop_array_item_for_agent",
            new_callable=AsyncMock,
        ) as wrapper:
            wrapper.return_value = {"ok": True, "item_index": 2, "old_item": {"x": 5}}
            result = await tool.coroutine(
                node_id="n_chart000",
                prop="data",
                match={"by_field": "label", "equals": "X"},
            )
        wrapper.assert_awaited_once_with(
            "p1", "n_chart000", "data", {"by_field": "label", "equals": "X"}
        )
        assert result["ok"] is True
        assert capture["ops"] == [
            {
                "op": "remove_prop_array_item",
                "args": {
                    "node_id": "n_chart000",
                    "prop": "data",
                    "match": {"by_field": "label", "equals": "X"},
                },
            }
        ]


class TestRunEditSpecialistSuccessFlag:
    """Lock down that ``ok`` reflects whether the backend stream actually
    completed. Before this guard, ``run_edit_specialist`` returned
    ``ok=True`` even when the inner backend errored mid-stream — the
    caller had no way to tell "no work needed" from "specialist
    crashed".

    All tests pass a non-None ``pocket`` — the runtime fail-fasts when
    pocket is missing (covered by TestRuntimeFailsFastWithoutPocket in
    test_edit_handoff.py), so success-flag tests need to clear that
    gate first.
    """

    _POCKET_FIXTURE = {"_id": "p1", "rippleSpec": {"state": {}, "ui": {"type": "flex"}}}

    @pytest.mark.asyncio
    async def test_ok_true_when_stream_completes(self) -> None:
        from unittest.mock import MagicMock

        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )
        from pocketpaw.agents.protocol import AgentEvent
        from pocketpaw.config import Settings

        async def _stream(*args, **kwargs):
            yield AgentEvent(type="message", content="done.")

        fake_backend = MagicMock()
        fake_backend.run = _stream
        fake_backend.attach_specialist_tools = MagicMock()
        fake_backend.stop = AsyncMock()

        with patch(
            "ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
            return_value=fake_backend,
        ):
            out = await run_edit_specialist(
                PocketSpecialistEditInput(
                    pocket_id="p1",
                    intent="rename row 1",
                    pocket=self._POCKET_FIXTURE,
                ),
                workspace_id="w1",
                user_id="u1",
                settings=Settings(),
            )

        assert out.ok is True
        assert out.error is None

    @pytest.mark.asyncio
    async def test_ok_false_when_backend_raises_mid_stream(self) -> None:
        """A transport drop / model 400 / any exception mid-stream must
        surface as ``ok=False`` with an error message, not a silent
        ``ok=True, ops=[]``."""
        from unittest.mock import MagicMock

        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )
        from pocketpaw.agents.protocol import AgentEvent
        from pocketpaw.config import Settings

        async def _exploding_stream(*args, **kwargs):
            yield AgentEvent(type="message", content="starting...")
            raise RuntimeError("DeepSeek 400: reasoning_content invalid")

        fake_backend = MagicMock()
        fake_backend.run = _exploding_stream
        fake_backend.attach_specialist_tools = MagicMock()
        fake_backend.stop = AsyncMock()

        with patch(
            "ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
            return_value=fake_backend,
        ):
            out = await run_edit_specialist(
                PocketSpecialistEditInput(
                    pocket_id="p1",
                    intent="rename row 1",
                    pocket=self._POCKET_FIXTURE,
                ),
                workspace_id="w1",
                user_id="u1",
                settings=Settings(),
            )

        assert out.ok is False
        assert out.error is not None
        assert "RuntimeError" in out.error
        assert "DeepSeek 400" in out.error
        # backend.stop must still run on the error path.
        fake_backend.stop.assert_awaited_once()


class TestPromptSeparation:
    def test_main_agent_interaction_prompt_is_thin(self) -> None:
        """Main agent's prompt should be the delegation variant —
        scope + canvas + delegation + current-pocket. No design rules,
        no mutation-strategy block."""
        from ee.ripple import POCKET_INTERACTION_PROMPT_MCP

        # Heavy blocks must be absent:
        assert "<mutation-strategy>" not in POCKET_INTERACTION_PROMPT_MCP
        assert "RIPPLE_DESIGN_RULES" not in POCKET_INTERACTION_PROMPT_MCP
        # The delegation rule must be present, naming the new tool:
        assert "pocket_specialist__edit" in POCKET_INTERACTION_PROMPT_MCP
        # Pocket-scope guardrails still apply:
        assert "<pocket-scope>" in POCKET_INTERACTION_PROMPT_MCP

    def test_edit_specialist_prompt_is_slim(self) -> None:
        """The specialist's prompt is deliberately small now — the
        parent agent sends the pocket payload and (optionally) target
        node ids, so the specialist needs only a granular-op cheat
        sheet, not the full design-rules block. Big prompts caused the
        model to hallucinate read tools and redesign the canvas."""
        from ee.ripple import (
            POCKET_EDIT_SPECIALIST_PROMPT_MCP,
            POCKET_INTERACTION_PROMPT_MCP,
        )

        # The block the slim prompt is built around must exist:
        assert "<edit-specialist>" in POCKET_EDIT_SPECIALIST_PROMPT_MCP
        # And the heavy design block + mutation-strategy block are GONE:
        assert "<mutation-strategy>" not in POCKET_EDIT_SPECIALIST_PROMPT_MCP
        # RIPPLE_DESIGN_RULES is no longer spliced in.
        assert "VISUAL VARIATION" not in POCKET_EDIT_SPECIALIST_PROMPT_MCP

        # Hard ceiling — the whole point of this change is to keep the
        # specialist prompt well under the size that caused trouble
        # (the old prompt was ~40k chars). 12k chars (~3k tokens) is a
        # comfortable upper bound for the slim version; raise this only
        # if a deliberate addition justifies it.
        assert len(POCKET_EDIT_SPECIALIST_PROMPT_MCP) < 12_000, (
            f"edit specialist prompt is {len(POCKET_EDIT_SPECIALIST_PROMPT_MCP)} "
            "chars; the one-shot redesign expects it to stay slim"
        )

        # Still in the same order of magnitude as the parent (both are
        # slim now) — the specialist no longer dwarfs the parent.
        ratio = len(POCKET_EDIT_SPECIALIST_PROMPT_MCP) / max(len(POCKET_INTERACTION_PROMPT_MCP), 1)
        assert ratio < 5, f"specialist/parent prompt ratio is {ratio:.1f}x; should be small now"


def test_edit_tool_bundle_includes_prop_array_item_tools():
    from ee.agent.pocket_specialist import tools

    bundle = tools.make_edit_pocket_tools(pocket_id="p1")
    names = {t.name for t in bundle}
    assert "set_prop_array_item" in names
    assert "append_prop_array_item" in names
    assert "remove_prop_array_item" in names
