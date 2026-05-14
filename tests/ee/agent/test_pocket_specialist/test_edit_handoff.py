"""Tests for the parent → edit-specialist one-shot handoff.

The parent (Claude) fetches the pocket once and ships the full payload
to the specialist (DeepSeek). The specialist has no read tool. These
tests lock the contract:

  * Edit input requires intent + pocket_id; pocket is the parent's
    contractual handoff and target_node_ids is optional.
  * MCP edit-tool schema marks pocket as required.
  * _build_edit_user_message inlines the pocket payload and surfaces
    target node ids when set.
  * Parent prompt teaches the one-shot flow (fetch → ship → done).
  * Specialist prompt teaches "no get_pocket; pocket is in the message".
"""

from __future__ import annotations


class TestEditInputAcceptsHandoff:
    def test_target_node_ids_optional_list(self) -> None:
        from ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

        inp = PocketSpecialistEditInput(
            pocket_id="p1",
            intent="rename the chart to Revenue Q4",
            target_node_ids=["n_chart00", "n_legend0"],
        )
        assert inp.target_node_ids == ["n_chart00", "n_legend0"]

    def test_pocket_handoff_dict(self) -> None:
        from ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

        inp = PocketSpecialistEditInput(
            pocket_id="p1",
            intent="filter to overdue",
            pocket={"_id": "p1", "rippleSpec": {"state": {"filter": "all"}}},
        )
        assert inp.pocket and inp.pocket["_id"] == "p1"

    def test_bare_input_still_constructs(self) -> None:
        """The Pydantic model still accepts a bare input — the runtime
        layer rejects it with a clear error rather than the model. Keeps
        the wire schema permissive for tests / fixtures while the
        runtime enforces the always-send-pocket contract."""
        from ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

        inp = PocketSpecialistEditInput(pocket_id="p1", intent="mark task 1 done")
        assert inp.pocket is None
        assert inp.target_node_ids is None


class TestUserMessageSurfacesHandoff:
    def test_target_node_ids_block_when_set(self) -> None:
        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            _build_edit_user_message,
        )

        msg = _build_edit_user_message(
            PocketSpecialistEditInput(
                pocket_id="p1",
                intent="rename the chart to Revenue Q4",
                pocket={"_id": "p1", "rippleSpec": {}},
                target_node_ids=["n_chart00"],
            )
        )
        assert "TARGET NODE IDS" in msg
        assert "n_chart00" in msg
        assert "authoritative" in msg.lower()

    def test_pocket_block_inlines_payload(self) -> None:
        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            _build_edit_user_message,
        )

        msg = _build_edit_user_message(
            PocketSpecialistEditInput(
                pocket_id="p1",
                intent="filter to overdue",
                pocket={"_id": "p1", "rippleSpec": {"state": {"filter": "all"}}},
            )
        )
        assert "CURRENT POCKET" in msg
        assert "rippleSpec" in msg
        # Tell the model not to look for a read tool.
        assert "no get_pocket" in msg.lower()
        # No "Read the pocket first" fallback in the new contract.
        assert "Read the pocket first" not in msg


class TestRuntimeFailsFastWithoutPocket:
    """The runtime refuses an edit run that did not receive the
    pocket payload. Cheaper than burning an LLM call doomed to noop."""

    async def test_run_returns_error_when_pocket_missing(self) -> None:
        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            run_edit_specialist,
        )
        from pocketpaw.config import Settings

        out = await run_edit_specialist(
            PocketSpecialistEditInput(pocket_id="p1", intent="add a stat widget"),
            workspace_id="w1",
            user_id="u1",
            settings=Settings(),
        )
        assert out.ok is False
        assert out.error is not None
        assert "pocket payload missing" in out.error
        assert out.ops == []


class TestMCPSchemaRequiresPocket:
    def test_edit_tool_schema_requires_pocket(self) -> None:
        """The MCP edit tool's args schema must mark `pocket` required
        so Claude's tool layer enforces the always-send contract."""
        from ee.agent.pocket_specialist import mcp_tool

        with open(mcp_tool.__file__, encoding="utf-8") as fh:
            text = fh.read()

        # Handler still pulls all three.
        assert 'args.get("pocket")' in text
        assert 'args.get("target_node_ids")' in text
        # And the schema lists pocket inside the required tuple.
        assert '"required": ["pocket_id", "intent", "pocket"]' in text
        assert '"target_node_ids"' in text
        assert '"items": {"type": "string"}' in text


class TestParentPromptOneShotFlow:
    def test_parent_prompt_describes_one_shot_flow(self) -> None:
        from ee.ripple._pockets import POCKET_INTERACTION_PROMPT_MCP

        assert "ONE-SHOT FLOW" in POCKET_INTERACTION_PROMPT_MCP

    def test_parent_prompt_mentions_target_node_ids(self) -> None:
        from ee.ripple._pockets import POCKET_INTERACTION_PROMPT_MCP

        assert "target_node_ids" in POCKET_INTERACTION_PROMPT_MCP

    def test_parent_prompt_shows_concrete_rich_edit_call(self) -> None:
        """The parent should see a worked example of an edit call with
        all four fields (pocket_id, intent, pocket, target_node_ids)."""
        from ee.ripple._pockets import POCKET_INTERACTION_PROMPT_MCP

        for field in ("pocket_id", "intent", "pocket", "target_node_ids"):
            assert f'"{field}"' in POCKET_INTERACTION_PROMPT_MCP

    def test_parent_prompt_marks_pocket_required(self) -> None:
        from ee.ripple._pockets import POCKET_INTERACTION_PROMPT_MCP

        # Some flag that the parent must always send pocket — not
        # locking to exact phrasing, but the word REQUIRED applied to
        # pocket should be there.
        assert "REQUIRED" in POCKET_INTERACTION_PROMPT_MCP

    def test_parent_prompt_caps_disambiguation_questions(self) -> None:
        from ee.ripple._pockets import POCKET_INTERACTION_PROMPT_MCP

        assert "Never more than one" in POCKET_INTERACTION_PROMPT_MCP


class TestSpecialistPromptHandoffRules:
    def test_specialist_prompt_teaches_no_get_pocket(self) -> None:
        from ee.ripple._pockets import POCKET_EDIT_SPECIALIST_PROMPT_MCP

        prompt = POCKET_EDIT_SPECIALIST_PROMPT_MCP
        assert "<edit-specialist>" in prompt
        # The specialist must be told it has no read tool, in any
        # phrasing — the slim prompt strips backticks for brevity.
        assert "no get_pocket" in prompt.lower() or "NO read tool" in prompt

    def test_specialist_prompt_teaches_target_node_ids_authoritative(self) -> None:
        from ee.ripple._pockets import POCKET_EDIT_SPECIALIST_PROMPT_MCP

        assert "TARGET NODE IDS" in POCKET_EDIT_SPECIALIST_PROMPT_MCP
        assert "AUTHORITATIVE" in POCKET_EDIT_SPECIALIST_PROMPT_MCP
        assert "work ONLY on these" in POCKET_EDIT_SPECIALIST_PROMPT_MCP
