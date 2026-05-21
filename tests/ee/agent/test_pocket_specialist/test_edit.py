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
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import (
            EDIT_TOOL_ID,
            POCKET_SPECIALIST_TOOL_IDS,
        )

        assert EDIT_TOOL_ID == "mcp__pocketpaw_pocket_specialist__edit"
        assert EDIT_TOOL_ID in POCKET_SPECIALIST_TOOL_IDS

    def test_create_and_edit_both_registered(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.mcp_tool import (
            CREATE_TOOL_ID,
            EDIT_TOOL_ID,
            POCKET_SPECIALIST_TOOL_IDS,
        )

        assert CREATE_TOOL_ID in POCKET_SPECIALIST_TOOL_IDS
        assert EDIT_TOOL_ID in POCKET_SPECIALIST_TOOL_IDS
        assert len(POCKET_SPECIALIST_TOOL_IDS) == 2


class TestEditInputOutput:
    def test_input_validates_pocket_id_and_intent(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

        ok = PocketSpecialistEditInput(pocket_id="p1", intent="rename row 1")
        assert ok.pocket_id == "p1"
        assert ok.intent == "rename row 1"

    def test_input_rejects_blank_intent(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            PocketSpecialistEditInput(pocket_id="p1", intent="hi")

    def test_input_rejects_empty_pocket_id(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            PocketSpecialistEditInput(pocket_id="", intent="add a button")

    def test_output_shape(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditOutput

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
        from pocketpaw_ee.agent.pocket_specialist.tools import make_edit_pocket_tools

        tools = make_edit_pocket_tools(pocket_id="p1")
        names = [t.name for t in tools]
        # The 10 granular ops the edit specialist gets.
        for expected in (
            "get_pocket",
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

    def test_pocket_id_is_closed_over_not_exposed_to_llm(self) -> None:
        """The LLM should NEVER see pocket_id as an argument — it's
        baked into the closure. Verify by checking the args_schema."""
        from pocketpaw_ee.agent.pocket_specialist.tools import make_set_state_tool

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

        from pocketpaw_ee.agent.pocket_specialist import tools as tools_mod
        from pocketpaw_ee.agent.pocket_specialist.tools import make_set_state_tool

        capture: dict = {}
        with patch.object(
            tools_mod,
            "_capture_op",
            wraps=tools_mod._capture_op,
        ) as wrapped:
            tool = make_set_state_tool(pocket_id="p1", capture=capture)
            with patch(
                "pocketpaw_ee.cloud.pockets.agent_context.set_state_for_agent",
                new=AsyncMock(return_value={"ok": True}),
            ):
                asyncio.run(tool.coroutine(path="filter", value="done"))
        wrapped.assert_called_once()
        assert capture.get("ops") == [
            {"op": "set_state", "args": {"path": "filter", "value": "done"}}
        ]


class TestRunEditSpecialistSuccessFlag:
    """Lock down that ``ok`` reflects whether the backend stream actually
    completed. Before this guard, ``run_edit_specialist`` returned
    ``ok=True`` even when the inner backend errored mid-stream — the
    caller had no way to tell "no work needed" from "specialist
    crashed"."""

    @pytest.mark.asyncio
    async def test_ok_true_when_stream_completes(self) -> None:
        from unittest.mock import MagicMock

        from pocketpaw_ee.agent.pocket_specialist.runtime import (
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
            "pocketpaw_ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
            return_value=fake_backend,
        ):
            out = await run_edit_specialist(
                PocketSpecialistEditInput(pocket_id="p1", intent="rename row 1"),
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

        from pocketpaw_ee.agent.pocket_specialist.runtime import (
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
            "pocketpaw_ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
            return_value=fake_backend,
        ):
            out = await run_edit_specialist(
                PocketSpecialistEditInput(pocket_id="p1", intent="rename row 1"),
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
        from pocketpaw.ripple import POCKET_INTERACTION_PROMPT_MCP

        # Heavy blocks must be absent:
        assert "<mutation-strategy>" not in POCKET_INTERACTION_PROMPT_MCP
        assert "RIPPLE_DESIGN_RULES" not in POCKET_INTERACTION_PROMPT_MCP
        # The delegation rule must be present, naming the new tool:
        assert "pocket_specialist__edit" in POCKET_INTERACTION_PROMPT_MCP
        # Pocket-scope guardrails still apply:
        assert "<pocket-scope>" in POCKET_INTERACTION_PROMPT_MCP

    def test_edit_specialist_prompt_is_heavy(self) -> None:
        """The specialist's prompt MUST carry the full mutation rules
        + design block — it's the agent actually doing edits."""
        from pocketpaw.ripple import POCKET_EDIT_SPECIALIST_PROMPT_MCP

        assert "<mutation-strategy>" in POCKET_EDIT_SPECIALIST_PROMPT_MCP
        # Should be substantially larger than the main agent's prompt.
        from pocketpaw.ripple import POCKET_INTERACTION_PROMPT_MCP

        assert len(POCKET_EDIT_SPECIALIST_PROMPT_MCP) > len(POCKET_INTERACTION_PROMPT_MCP) * 5, (
            "edit specialist prompt should dwarf the thin main-agent prompt"
        )
