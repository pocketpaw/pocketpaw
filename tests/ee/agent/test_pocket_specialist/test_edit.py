"""Smoke tests for the edit specialist surface.

Covers the public wiring:
  * mcp_tool exports EDIT_TOOL_ID and registers a sibling MCP tool
  * runtime imports + input/output models work as advertised
  * tool factories produce StructuredTool objects with the right names
  * main-agent interaction prompt is the thin delegation variant;
    specialist prompt is the heavy variant

Regression tests for #1163 (silent 0-ops edits):
  * Root cause A — backend yields AgentEvent(type="error") without raising;
    runtime must surface ok=False, not ok=True.
  * Root cause B — edit specialist system prompt must name the granular edit
    tools it actually holds and must NOT advertise create_pocket / update_pocket
    / add_widget.
  * Decline path — a 0-ops run with no error must surface the planner's
    reply in ``warnings`` rather than a silent ok=True.
  * Rejected op — a service-rejected granular op must not count as applied;
    its error is folded into ``warnings`` (PR #1165 review finding #2).

Regression test for edit-ignores-agent-mode bug (#1170):
  * In agent mode (POCKETPAW_POCKET_SPECIALIST_MODE=agent), pocket CREATE goes
    through AgentModeAdapter which never spawns a sub-agent backend.  Pocket
    EDIT ignores the mode setting entirely and always calls
    AgentRouter.create_isolated_backend — which needs ANTHROPIC_API_KEY when
    the default ``deep_agents`` backend is selected, crashing with
    ``TypeError: Could not resolve authentication method`` on Claude Code
    deployments.
  * Post-fix contract: run_edit_specialist in agent mode MUST NOT call
    create_isolated_backend.
  * Agent-mode two-call protocol coverage: the first call (no ``ops``)
    returns a draft kit; the second call (``ops`` populated) applies the
    chat agent's granular ops through the real edit tools — rejected and
    unknown ops fold into ``warnings`` like the subagent path.
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
        # The granular ops the edit specialist gets — including the
        # Tier-2 surgical prop-array item ops.
        for expected in (
            "get_pocket",
            "set_state",
            "append_state",
            "remove_state",
            "patch_state",
            "set_node_prop",
            "set_prop_array_item",
            "append_prop_array_item",
            "remove_prop_array_item",
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


class TestPropArrayItemToolFactories:
    async def test_set_prop_array_item_tool_invokes_wrapper(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist import tools

        capture: dict = {}
        tool = tools.make_set_prop_array_item_tool(pocket_id="p1", capture=capture)
        with patch(
            "pocketpaw_ee.cloud.pockets.agent_context.set_prop_array_item_for_agent",
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
        from pocketpaw_ee.agent.pocket_specialist import tools

        capture: dict = {}
        tool = tools.make_append_prop_array_item_tool(pocket_id="p1", capture=capture)
        with patch(
            "pocketpaw_ee.cloud.pockets.agent_context.append_prop_array_item_for_agent",
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
        from pocketpaw_ee.agent.pocket_specialist import tools

        capture: dict = {}
        tool = tools.make_remove_prop_array_item_tool(pocket_id="p1", capture=capture)
        with patch(
            "pocketpaw_ee.cloud.pockets.agent_context.remove_prop_array_item_for_agent",
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


def test_edit_tool_bundle_includes_prop_array_item_tools():
    from pocketpaw_ee.agent.pocket_specialist import tools

    bundle = tools.make_edit_pocket_tools(pocket_id="p1")
    names = {t.name for t in bundle}
    assert "set_prop_array_item" in names
    assert "append_prop_array_item" in names
    assert "remove_prop_array_item" in names


class TestRunEditSpecialistSuccessFlag:
    """Lock down that ``ok`` reflects whether the backend stream actually
    completed. Before this guard, ``run_edit_specialist`` returned
    ``ok=True`` even when the inner backend errored mid-stream — the
    caller had no way to tell "no work needed" from "specialist
    crashed".

    These tests exercise the SUBAGENT pipeline (the backend-spawn path),
    so they pin ``pocket_specialist_mode="subagent"`` explicitly rather
    than relying on the ambient ``Settings()`` default — that default is
    overridable by env / ``.env`` (#1170 added the ``agent`` mode edit
    path, which never spawns a backend)."""

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
                settings=Settings(pocket_specialist_mode="subagent"),
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
                settings=Settings(pocket_specialist_mode="subagent"),
            )

        assert out.ok is False
        assert out.error is not None
        assert "RuntimeError" in out.error
        assert "DeepSeek 400" in out.error
        # backend.stop must still run on the error path.
        fake_backend.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ok_false_when_backend_yields_error_event(self) -> None:
        """#1163 root cause A — the deep_agents backend NEVER raises on error.
        Instead it yields AgentEvent(type="error", ...) followed by
        AgentEvent(type="done").  The runtime loop only inspects
        ``event.type == "tool_use"`` so the error event passes silently, the
        loop exits cleanly, ``success`` flips to ``True``, and the caller
        gets ``ok=True, ops=[], error=None``.

        Post-fix contract: a yielded error event must produce
        ``ok=False`` with ``error`` populated — indistinguishable from a
        raised exception from the caller's perspective.

        This test FAILS today (current code returns ok=True, error=None).
        """
        from unittest.mock import MagicMock

        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )

        from pocketpaw.agents.protocol import AgentEvent
        from pocketpaw.config import Settings

        # Mirrors the exact pattern in deep_agents.py:974-977 — no raise,
        # just two yield statements: error then done.
        async def _deep_agents_error_stream(*args, **kwargs):
            yield AgentEvent(type="message", content="planning edit...")
            yield AgentEvent(
                type="error",
                content="Deep Agents error: context length exceeded for 12KB spec",
            )
            yield AgentEvent(type="done", content="")

        fake_backend = MagicMock()
        fake_backend.run = _deep_agents_error_stream
        fake_backend.attach_specialist_tools = MagicMock()
        fake_backend.stop = AsyncMock()

        with patch(
            "pocketpaw_ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
            return_value=fake_backend,
        ):
            out = await run_edit_specialist(
                PocketSpecialistEditInput(
                    pocket_id="6a0eb47ac0dd58069139b985",
                    intent="replace team[3] Gaurv (handle TBD) with Gaurav Dewani (dewani12)",
                ),
                workspace_id="69e4f93b57ff64b3903868e3",
                user_id="69d0f4d09dfb6ddfd8e2da84",
                settings=Settings(pocket_specialist_mode="subagent"),
            )

        # POST-FIX expectation (fails today — current code returns ok=True):
        assert out.ok is False, (
            f"Expected ok=False when backend yields error event, got ok={out.ok} "
            f"error={out.error!r} — this is #1163 root cause A"
        )
        assert out.error is not None, "error field must be populated with backend error message"
        assert "context length exceeded" in out.error, (
            f"error should contain the backend's message, got: {out.error!r}"
        )
        # ops must be empty — no work was done.
        assert out.ops == []
        # backend.stop must still run regardless.
        fake_backend.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_decline_run_surfaces_planner_reply_in_warnings(self) -> None:
        """#1163 headline contract — the planner reads the pocket, decides
        no granular op is warranted, and replies with plain text. NO
        tool_use event is emitted, so ``ops`` is empty.

        Before the fix this returned ``ok=True, ops=[], warnings=[]`` — a
        silent success indistinguishable from a real edit. Post-fix the
        run is still ``ok=True`` (nothing failed) but ``warnings`` carries
        the planner's reply so the caller can tell the user WHY nothing
        changed.
        """
        from unittest.mock import MagicMock

        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )

        from pocketpaw.agents.protocol import AgentEvent
        from pocketpaw.config import Settings

        # deep_agents emits message events as token-level chunks — model
        # that here so the test also pins the ""-join (no choppy prose).
        async def _decline_stream(*args, **kwargs):
            yield AgentEvent(type="message", content="The team member ")
            yield AgentEvent(type="message", content="you named already ")
            yield AgentEvent(type="message", content="matches the current value.")
            yield AgentEvent(type="done", content="")

        fake_backend = MagicMock()
        fake_backend.run = _decline_stream
        fake_backend.attach_specialist_tools = MagicMock()
        fake_backend.stop = AsyncMock()

        with patch(
            "pocketpaw_ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
            return_value=fake_backend,
        ):
            out = await run_edit_specialist(
                PocketSpecialistEditInput(
                    pocket_id="6a0eb47ac0dd58069139b985",
                    intent="rename team member 4 to the name they already have",
                ),
                workspace_id="69e4f93b57ff64b3903868e3",
                user_id="69d0f4d09dfb6ddfd8e2da84",
                settings=Settings(pocket_specialist_mode="subagent"),
            )

        # A decline is NOT a failure — the run completed cleanly.
        assert out.ok is True
        assert out.error is None
        # No granular op ran.
        assert out.ops == []
        # The headline contract — the reason reaches the caller.
        assert out.warnings, "a 0-ops run must surface a reason in warnings"
        joined = " ".join(out.warnings)
        assert "matches the current value" in joined, (
            f"warnings must carry the planner's reply, got: {out.warnings!r}"
        )
        # Token chunks joined with "" — clean prose, no newline soup.
        assert "The team member you named already matches the current value." in joined, (
            f"message chunks must join cleanly, got: {out.warnings!r}"
        )
        fake_backend.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_service_rejected_op_not_counted_and_surfaced_in_warnings(self) -> None:
        """#1165 review finding #2 — a granular op the service REJECTS
        (``{ok: false}``) must not count as applied. Before the fix the op
        still landed in ``ops``, so a run whose only op was rejected
        returned ``ok=True, ops=[<rejected op>]`` — another silent failure
        of the #1163 class.

        Post-fix: the rejected op is absent from ``ops`` and its error is
        surfaced in ``warnings``. The run is ``ok=True`` (nothing crashed)
        but the caller can see the edit did not land.
        """
        from unittest.mock import MagicMock

        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )

        from pocketpaw.agents.protocol import AgentEvent
        from pocketpaw.config import Settings

        captured_tools: dict = {}

        def _grab_tools(tools):
            captured_tools["tools"] = {t.name: t for t in tools}

        # The fake backend invokes a real granular tool mid-stream — the
        # same StructuredTool the runtime attached. The tool's wrapper hits
        # set_state_for_agent, which we patch to reject the write.
        async def _rejecting_stream(*args, **kwargs):
            yield AgentEvent(type="message", content="applying the edit...")
            tool = captured_tools["tools"]["set_state"]
            yield AgentEvent(
                type="tool_use",
                content="Using set_state...",
                metadata={"name": "set_state", "input": {}},
            )
            await tool.coroutine(path="filter", value="overdue")
            yield AgentEvent(type="done", content="")

        fake_backend = MagicMock()
        fake_backend.run = _rejecting_stream
        fake_backend.attach_specialist_tools = MagicMock(side_effect=_grab_tools)
        fake_backend.stop = AsyncMock()

        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
                return_value=fake_backend,
            ),
            patch(
                "pocketpaw_ee.cloud.pockets.agent_context.set_state_for_agent",
                new=AsyncMock(
                    return_value={"ok": False, "error": "state path 'filter' does not exist"}
                ),
            ),
        ):
            out = await run_edit_specialist(
                PocketSpecialistEditInput(
                    pocket_id="6a0eb47ac0dd58069139b985",
                    intent="filter to overdue only",
                ),
                workspace_id="69e4f93b57ff64b3903868e3",
                user_id="69d0f4d09dfb6ddfd8e2da84",
                settings=Settings(pocket_specialist_mode="subagent"),
            )

        # The run completed — nothing raised.
        assert out.ok is True
        assert out.error is None
        # A rejected op is NOT an applied op — it must not be in ops.
        assert out.ops == [], (
            f"a service-rejected op must not count as applied, got ops={out.ops!r}"
        )
        # The rejection reason must reach the caller.
        assert out.warnings, "a rejected op must surface its reason in warnings"
        joined = " ".join(out.warnings)
        assert "set_state" in joined and "does not exist" in joined, (
            f"warnings must name the rejected op and its error, got: {out.warnings!r}"
        )
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

    def test_edit_specialist_prompt_names_granular_tools_not_creation_tools(self) -> None:
        """#1163 root cause B — the edit specialist's system prompt is assembled
        by ``_assemble_interaction`` which splices in ``_TOOLS_MCP``.  That
        block advertises ``create_pocket``, ``update_pocket``, and ``add_widget``
        — tools the edit specialist does NOT hold — and makes zero mention of
        the granular edit ops the specialist actually holds (including the
        Tier-2 array-item ops added in PR #1159).

        When the LLM is told it has ``create_pocket`` but not
        ``set_prop_array_item``, it either picks the wrong tool or declines to
        act and emits 0 ops — exactly the silent-success pattern in #1163.

        Post-fix contract:
          (a) The prompt NAMES each granular edit tool the specialist holds —
              at minimum: set_prop_array_item, append_prop_array_item,
              remove_prop_array_item, set_node_prop, add_node.
          (b) The prompt does NOT advertise create_pocket, update_pocket, or
              add_widget as available tools.

        This test FAILS today (0 mentions of the array-item ops in the prompt).
        """
        from pocketpaw.ripple import POCKET_EDIT_SPECIALIST_PROMPT_MCP

        prompt = POCKET_EDIT_SPECIALIST_PROMPT_MCP

        # --- (a) granular edit tools the specialist DOES hold ---
        # These are the tools from make_edit_pocket_tools() — the edit
        # specialist's actual tool surface, including the Tier-2 array-item
        # ops from PR #1159.
        required_tools = [
            "set_prop_array_item",
            "append_prop_array_item",
            "remove_prop_array_item",
            "set_node_prop",
            "add_node",
        ]
        missing = [t for t in required_tools if t not in prompt]
        assert not missing, (
            f"Edit specialist prompt is missing these granular tool names: {missing}\n"
            "The LLM cannot use tools it is not told about — "
            "this is #1163 root cause B."
        )

        # --- (b) creation tools the specialist does NOT hold ---
        # These should NOT appear as available tool calls.  Their presence
        # causes the LLM to attempt the wrong tool and receive no result,
        # producing 0 ops silently.
        forbidden_as_available = ["create_pocket", "update_pocket", "add_widget"]
        # Allow the words to appear in HARD RULES / prohibition text
        # (e.g. "NEVER call create_pocket") — those are correct guard rails.
        # What we're checking is that they do NOT appear in a tool-surface
        # description (i.e. inside a <pocket-tools> / <specialist-tools> block
        # that describes what the specialist can call).
        import re

        # Extract the tools block from the prompt — look for the first
        # <pocket-tools> ... </pocket-tools> section.
        tools_block_match = re.search(r"<pocket-tools>(.*?)</pocket-tools>", prompt, re.DOTALL)
        assert tools_block_match is not None, (
            "Edit specialist prompt has no <pocket-tools> block — cannot verify tool surface"
        )
        tools_block = tools_block_match.group(1)

        for forbidden in forbidden_as_available:
            assert forbidden not in tools_block, (
                f"Edit specialist prompt's <pocket-tools> block advertises "
                f"'{forbidden}' — a creation tool the specialist does NOT hold. "
                "This causes the LLM to attempt the wrong tool and produce 0 ops "
                "(#1163 root cause B). Remove it from the tools block; "
                "prohibition text in HARD RULES is still fine."
            )


class TestAgentModeEditDispatch:
    """Regression tests for the bug where run_edit_specialist ignores
    pocket_specialist_mode and always spawns an isolated backend even when
    mode is ``agent``.

    The create path (run_specialist) correctly dispatches through pick_adapter:
    when mode is ``agent``, AgentModeAdapter handles the call without ever
    touching AgentRouter.create_isolated_backend.

    The edit path has no equivalent dispatch — it calls create_isolated_backend
    unconditionally at runtime.py line ~578. With the default
    pocket_specialist_backend=deep_agents, that backend's __init__ reaches
    LangChain ChatAnthropic, which raises
    ``TypeError: Could not resolve authentication method`` when ANTHROPIC_API_KEY
    is absent (e.g., on a Claude Code deployment).

    Post-fix contract: in agent mode run_edit_specialist must NOT call
    create_isolated_backend.
    """

    @pytest.mark.asyncio
    async def test_agent_mode_edit_does_not_spawn_isolated_backend(self) -> None:
        """Bug reproduction: run_edit_specialist with pocket_specialist_mode='agent'
        ALWAYS calls AgentRouter.create_isolated_backend today, ignoring the mode.

        Post-fix contract: when mode is 'agent', the edit path mirrors the create
        path's AgentModeAdapter and returns without ever calling create_isolated_backend.

        This test FAILS today because create_isolated_backend is called unconditionally
        in run_edit_specialist regardless of pocket_specialist_mode.
        """

        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )

        from pocketpaw.config import Settings

        settings = Settings(pocket_specialist_mode="agent")
        # Confirm the setting is actually agent mode (belt-and-suspenders)
        assert settings.pocket_specialist_mode == "agent"

        with patch(
            "pocketpaw_ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
        ) as mock_create_backend:
            # In agent mode the edit path should handle the request without
            # spawning a backend. We do NOT set a return value on mock_create_backend
            # because it must never be called — if it is called the test fails on
            # assert_not_called() below.  We also don't need to configure a fake
            # streaming backend because the agent-mode path should exit before
            # reaching the backend.run() loop.
            try:
                await run_edit_specialist(
                    PocketSpecialistEditInput(
                        pocket_id="p1",
                        intent="rename the first row to 'Active'",
                    ),
                    workspace_id="w1",
                    user_id="u1",
                    settings=settings,
                )
            except Exception:
                # The agent-mode path may raise NotImplementedError or similar
                # while it is being built — that is acceptable.  What is NOT
                # acceptable is reaching create_isolated_backend at all.
                pass

        (
            mock_create_backend.assert_not_called(),
            (
                "run_edit_specialist called AgentRouter.create_isolated_backend even though "
                "pocket_specialist_mode='agent'. In agent mode the edit path must NOT spawn "
                "a sub-agent backend — doing so triggers "
                "TypeError: Could not resolve authentication method when ANTHROPIC_API_KEY "
                "is absent (the bug on Claude Code deployments). "
                "Fix: add mode-dispatch to run_edit_specialist mirroring run_specialist's "
                "pick_adapter call so agent mode takes an adapter path that skips the backend."
            ),
        )

    @pytest.mark.asyncio
    async def test_subagent_mode_edit_still_spawns_backend(self) -> None:
        """Companion guard: subagent mode (the default) must CONTINUE to spawn
        an isolated backend after the agent-mode fix lands.

        This test must PASS today and must continue to pass after the fix.
        It verifies that the fix doesn't accidentally break the subagent path.
        """
        from unittest.mock import MagicMock

        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )

        from pocketpaw.agents.protocol import AgentEvent
        from pocketpaw.config import Settings

        settings = Settings(pocket_specialist_mode="subagent")

        async def _noop_stream(*args, **kwargs):
            yield AgentEvent(type="done", content="")

        fake_backend = MagicMock()
        fake_backend.run = _noop_stream
        fake_backend.attach_specialist_tools = MagicMock()
        fake_backend.stop = AsyncMock()

        with patch(
            "pocketpaw_ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
            return_value=fake_backend,
        ) as mock_create_backend:
            await run_edit_specialist(
                PocketSpecialistEditInput(
                    pocket_id="p1",
                    intent="rename the first row to 'Active'",
                ),
                workspace_id="w1",
                user_id="u1",
                settings=settings,
            )

        (
            mock_create_backend.assert_called_once(),
            (
                "subagent mode must still call create_isolated_backend — "
                "the agent-mode fix must not break the existing subagent path."
            ),
        )

    @pytest.mark.asyncio
    async def test_agent_mode_first_call_returns_draft_kit(self) -> None:
        """Agent-mode first call (no ``ops``) must return a draft kit so
        the chat agent knows how to compute the granular ops — mirroring
        create's ``action='draft_kit'`` first call. No backend spawned."""
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )

        from pocketpaw.config import Settings

        with patch(
            "pocketpaw_ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
        ) as mock_create_backend:
            out = await run_edit_specialist(
                PocketSpecialistEditInput(pocket_id="p1", intent="rename the first row"),
                workspace_id="w1",
                user_id="u1",
                settings=Settings(pocket_specialist_mode="agent"),
            )

        mock_create_backend.assert_not_called()
        assert out.action == "draft_kit"
        assert out.ok is False
        assert out.backend_used == "agent_mode"
        assert out.draft_kit is not None
        # The kit must name the granular op vocabulary.
        assert "set_state" in out.draft_kit["granular_ops"]
        assert "add_node" in out.draft_kit["granular_ops"]
        assert out.ops == []

    @pytest.mark.asyncio
    async def test_agent_mode_second_call_applies_ops_without_backend(self) -> None:
        """Agent-mode second call (``ops`` populated) must apply the chat
        agent's granular ops through the real edit tools — no backend
        spawned, no LLM. Mirrors create's validate-and-persist second call."""
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )

        from pocketpaw.config import Settings

        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
            ) as mock_create_backend,
            patch(
                "pocketpaw_ee.cloud.pockets.agent_context.set_state_for_agent",
                new=AsyncMock(return_value={"ok": True}),
            ),
        ):
            out = await run_edit_specialist(
                PocketSpecialistEditInput(
                    pocket_id="p1",
                    intent="filter to overdue",
                    ops=[{"op": "set_state", "args": {"path": "filter", "value": "overdue"}}],
                ),
                workspace_id="w1",
                user_id="u1",
                settings=Settings(pocket_specialist_mode="agent"),
            )

        mock_create_backend.assert_not_called()
        assert out.ok is True
        assert out.action == "applied"
        assert out.backend_used == "agent_mode"
        assert out.ops == [{"op": "set_state", "args": {"path": "filter", "value": "overdue"}}]
        assert out.error is None

    @pytest.mark.asyncio
    async def test_agent_mode_rejected_op_surfaces_in_warnings(self) -> None:
        """A service-rejected op in agent mode must NOT count as applied —
        its reason folds into ``warnings``, same contract as subagent mode
        (#1163 class).

        When the rejected op was the ONLY op, zero ops applied — the run
        reports ``ok=False, action="failed"`` so the caller can't mistake
        a no-change run for a successful edit (the agent-mode root-replace
        silent-failure class). ``warnings`` still carries the per-op
        reason; ``error`` explains nothing applied."""
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )

        from pocketpaw.config import Settings

        with (
            patch(
                "pocketpaw_ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
            ) as mock_create_backend,
            patch(
                "pocketpaw_ee.cloud.pockets.agent_context.set_state_for_agent",
                new=AsyncMock(
                    return_value={"ok": False, "error": "state path 'filter' does not exist"}
                ),
            ),
        ):
            out = await run_edit_specialist(
                PocketSpecialistEditInput(
                    pocket_id="p1",
                    intent="filter to overdue",
                    ops=[{"op": "set_state", "args": {"path": "filter", "value": "overdue"}}],
                ),
                workspace_id="w1",
                user_id="u1",
                settings=Settings(pocket_specialist_mode="agent"),
            )

        mock_create_backend.assert_not_called()
        assert out.ok is False, "a run that applied zero ops is not a success"
        assert out.action == "failed"
        assert out.ops == []
        assert out.error, "a no-op-applied run must explain why in error"
        assert out.warnings, "a rejected op must surface its reason in warnings"
        joined = " ".join(out.warnings)
        assert "set_state" in joined and "does not exist" in joined

    @pytest.mark.asyncio
    async def test_agent_mode_unknown_op_surfaces_in_warnings(self) -> None:
        """An op naming a tool the specialist does not hold must be skipped
        and reported in ``warnings`` — not crash the run.

        When the unknown op was the ONLY op, zero ops applied — the run
        reports ``ok=False, action="failed"`` (zero-ops-applied is not a
        success). ``warnings`` still names the skipped op."""
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )

        from pocketpaw.config import Settings

        with patch(
            "pocketpaw_ee.agent.pocket_specialist.runtime.AgentRouter.create_isolated_backend",
        ) as mock_create_backend:
            out = await run_edit_specialist(
                PocketSpecialistEditInput(
                    pocket_id="p1",
                    intent="do something unsupported",
                    ops=[{"op": "create_pocket", "args": {}}],
                ),
                workspace_id="w1",
                user_id="u1",
                settings=Settings(pocket_specialist_mode="agent"),
            )

        mock_create_backend.assert_not_called()
        assert out.ok is False, "a run that applied zero ops is not a success"
        assert out.action == "failed"
        assert out.ops == []
        assert out.error
        assert out.warnings
        assert "create_pocket" in " ".join(out.warnings)
