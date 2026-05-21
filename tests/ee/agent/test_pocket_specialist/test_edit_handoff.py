"""Tests for the parent → edit-specialist handoff.

The parent (Claude) decides WHAT and WHERE; the specialist (DeepSeek)
applies the change. These tests lock the contract:

  * Edit input accepts optional pocket + target_node_ids.
  * MCP edit-tool schema advertises both as optional.
  * _build_edit_user_message surfaces them prominently when set.
  * Specialist prompt teaches the skip-read behavior.
  * Parent prompt teaches the 3-way edit decision tree.
"""

from __future__ import annotations


class TestEditInputAcceptsHandoff:
    def test_target_node_ids_optional_list(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

        inp = PocketSpecialistEditInput(
            pocket_id="p1",
            intent="rename the chart to Revenue Q4",
            target_node_ids=["n_chart00", "n_legend0"],
        )
        assert inp.target_node_ids == ["n_chart00", "n_legend0"]

    def test_pocket_optional_dict(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

        inp = PocketSpecialistEditInput(
            pocket_id="p1",
            intent="filter to overdue",
            pocket={"_id": "p1", "rippleSpec": {"state": {"filter": "all"}}},
        )
        assert inp.pocket and inp.pocket["_id"] == "p1"

    def test_bare_input_still_valid(self) -> None:
        """Backwards-compat: just pocket_id + intent still works."""
        from pocketpaw_ee.agent.pocket_specialist.runtime import PocketSpecialistEditInput

        inp = PocketSpecialistEditInput(pocket_id="p1", intent="mark task 1 done")
        assert inp.pocket is None
        assert inp.target_node_ids is None


class TestUserMessageSurfacesHandoff:
    def test_target_node_ids_block_when_set(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            _build_edit_user_message,
        )

        msg = _build_edit_user_message(
            PocketSpecialistEditInput(
                pocket_id="p1",
                intent="rename the chart to Revenue Q4",
                target_node_ids=["n_chart00"],
            )
        )
        assert "TARGET NODE IDS" in msg
        assert "n_chart00" in msg
        # Without pocket, the read-first prompt should NOT appear since
        # target node ids alone are enough to act on.
        assert "Read the pocket first" not in msg

    def test_pocket_block_when_set(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
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
        # The pocket payload must be inline (skip-read instruction is the point).
        assert "rippleSpec" in msg
        assert "skip" in msg.lower() or "use this directly" in msg

    def test_read_first_when_neither_handoff_field_set(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            _build_edit_user_message,
        )

        msg = _build_edit_user_message(
            PocketSpecialistEditInput(pocket_id="p1", intent="add a stat widget")
        )
        assert "Read the pocket first" in msg

    def test_both_handoff_fields_omit_read_first(self) -> None:
        from pocketpaw_ee.agent.pocket_specialist.runtime import (
            PocketSpecialistEditInput,
            _build_edit_user_message,
        )

        msg = _build_edit_user_message(
            PocketSpecialistEditInput(
                pocket_id="p1",
                intent="rename the chart",
                pocket={"x": 1},
                target_node_ids=["n_chart00"],
            )
        )
        assert "Read the pocket first" not in msg
        assert "TARGET NODE IDS" in msg
        assert "CURRENT POCKET" in msg


class TestMCPSchemaAdvertisesHandoff:
    def test_edit_tool_schema_includes_handoff_fields(self) -> None:
        """The MCP edit tool's args schema must advertise both
        optional handoff fields so Claude's tool layer surfaces them."""
        from pocketpaw_ee.agent.pocket_specialist import mcp_tool

        with open(mcp_tool.__file__, encoding="utf-8") as fh:
            text = fh.read()

        # Both fields named in the edit handler arg pass-through.
        assert 'args.get("pocket")' in text
        assert 'args.get("target_node_ids")' in text
        # And advertised in the schema's properties block.
        # Two `"pocket"` strings (handler + schema) is fine; we want at
        # least one schema-style entry with array items for target_node_ids.
        assert '"target_node_ids"' in text
        assert '"items": {"type": "string"}' in text


class TestParentPromptEditDecisionTree:
    def test_parent_prompt_has_decision_tree(self) -> None:
        from pocketpaw.ripple._pockets import POCKET_INTERACTION_PROMPT_MCP

        assert "EDIT DECISION TREE" in POCKET_INTERACTION_PROMPT_MCP
        # Three explicit branches:
        assert "Type A" in POCKET_INTERACTION_PROMPT_MCP
        assert "Type B" in POCKET_INTERACTION_PROMPT_MCP
        assert "Type C" in POCKET_INTERACTION_PROMPT_MCP

    def test_parent_prompt_mentions_target_node_ids(self) -> None:
        from pocketpaw.ripple._pockets import POCKET_INTERACTION_PROMPT_MCP

        assert "target_node_ids" in POCKET_INTERACTION_PROMPT_MCP

    def test_parent_prompt_shows_concrete_rich_edit_call(self) -> None:
        """The parent should see a worked example of an edit call with
        all four fields (pocket_id, intent, pocket, target_node_ids)."""
        from pocketpaw.ripple._pockets import POCKET_INTERACTION_PROMPT_MCP

        for field in ("pocket_id", "intent", "pocket", "target_node_ids"):
            assert f'"{field}"' in POCKET_INTERACTION_PROMPT_MCP

    def test_parent_prompt_caps_disambiguation_questions(self) -> None:
        from pocketpaw.ripple._pockets import POCKET_INTERACTION_PROMPT_MCP

        assert "NEVER ask more than 1 disambiguation question" in POCKET_INTERACTION_PROMPT_MCP


class TestSpecialistPromptHandoffRules:
    def test_specialist_prompt_teaches_skip_read_when_pocket_passed(self) -> None:
        from pocketpaw.ripple._pockets import POCKET_EDIT_SPECIALIST_PROMPT_MCP

        assert "<parent-handoff>" in POCKET_EDIT_SPECIALIST_PROMPT_MCP
        assert "SKIP your own `get_pocket`" in POCKET_EDIT_SPECIALIST_PROMPT_MCP

    def test_specialist_prompt_teaches_target_node_ids_authoritative(self) -> None:
        from pocketpaw.ripple._pockets import POCKET_EDIT_SPECIALIST_PROMPT_MCP

        # Authoritative — don't search past the parent's lookup.
        assert "TARGET NODE IDS" in POCKET_EDIT_SPECIALIST_PROMPT_MCP
        assert "authoritative" in POCKET_EDIT_SPECIALIST_PROMPT_MCP.lower()
        assert "Work ONLY on these" in POCKET_EDIT_SPECIALIST_PROMPT_MCP
