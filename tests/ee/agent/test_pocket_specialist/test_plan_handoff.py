"""Tests for the parent → specialist plan handoff.

The parent agent (Claude) does design thinking; the specialist
(DeepSeek) does faithful translation. These tests lock the contract:

  * Hints model accepts the new plan fields.
  * MCP tool args schema advertises them with the right enum/types.
  * _build_user_message surfaces plan fields prominently.
  * _build_system_prompt segregates surface metadata from plan in
    distinct labelled blocks.
  * Parent prompt teaches the two-phase delegation flow.
  * Specialist prompt teaches "follow the plan, don't redesign".
"""

from __future__ import annotations


class TestHintsAcceptPlan:
    def test_all_plan_fields_optional(self) -> None:
        from ee.agent.pocket_specialist.runtime import PocketSpecialistHints

        # Default construct — no fields required.
        h = PocketSpecialistHints()
        for f in ("purpose", "layout", "focal_widget", "data_shape", "key_interactions"):
            assert getattr(h, f) is None

    def test_full_plan_passes_through(self) -> None:
        from ee.agent.pocket_specialist.runtime import PocketSpecialistHints

        h = PocketSpecialistHints(
            name="Sales Command Center",
            color="#4f46e5",
            purpose="Track quarterly sales pipeline at a glance",
            layout="hero+grid",
            focal_widget="data-grid",
            data_shape={
                "deals": "[{id, account, stage, value, owner, close_date}]",
                "filter": "string",
            },
            key_interactions=["filter deals by stage", "sort by value"],
        )
        assert h.layout == "hero+grid"
        assert h.focal_widget == "data-grid"
        assert h.data_shape and "deals" in h.data_shape
        assert h.key_interactions and len(h.key_interactions) == 2


class TestUserMessageSurfacesPlan:
    def test_plan_appended_when_set(self) -> None:
        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateInput,
            PocketSpecialistHints,
            _build_user_message,
        )

        msg = _build_user_message(
            PocketSpecialistCreateInput(
                brief="A pocket for tracking quarterly sales.",
                hints=PocketSpecialistHints(
                    layout="hero+grid",
                    focal_widget="data-grid",
                    key_interactions=["filter by stage"],
                ),
            )
        )
        assert "PLAN (from parent agent" in msg
        assert "layout: hero+grid" in msg
        assert "focal_widget: data-grid" in msg

    def test_no_plan_block_when_only_surface_metadata(self) -> None:
        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateInput,
            PocketSpecialistHints,
            _build_user_message,
        )

        msg = _build_user_message(
            PocketSpecialistCreateInput(
                brief="A simple pocket without any structural plan.",
                hints=PocketSpecialistHints(name="X", color="#000000"),
            )
        )
        assert "PLAN (from parent agent" not in msg

    def test_no_plan_block_when_no_hints(self) -> None:
        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistCreateInput,
            _build_user_message,
        )

        msg = _build_user_message(PocketSpecialistCreateInput(brief="A simple pocket."))
        assert "PLAN" not in msg


class TestSystemPromptSegregation:
    def test_surface_and_plan_in_separate_labeled_blocks(self) -> None:
        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistHints,
            _build_system_prompt,
        )

        prompt = _build_system_prompt(
            PocketSpecialistHints(
                name="Sales",
                color="#000000",
                layout="hero+grid",
                focal_widget="data-grid",
            )
        )
        assert "CALLER METADATA" in prompt
        assert "STRUCTURAL PLAN FROM PARENT AGENT" in prompt
        # And both blocks appear with the right values.
        assert "name: Sales" in prompt
        assert "layout: hero+grid" in prompt
        # The "don't redesign" reminder is co-located with the plan.
        assert "do not redesign" in prompt.lower() or "not creative" in prompt.lower()

    def test_empty_hints_appends_nothing(self) -> None:
        from ee.agent.pocket_specialist.runtime import (
            PocketSpecialistHints,
            _build_system_prompt,
        )

        prompt = _build_system_prompt(PocketSpecialistHints())
        assert "CALLER METADATA" not in prompt
        assert "STRUCTURAL PLAN" not in prompt


class TestMCPSchemaAdvertisesPlan:
    def test_schema_includes_plan_fields_with_correct_types(self) -> None:
        """The schema string the MCP tool registers must advertise the
        plan fields with valid JSON schema types so Claude's tool-use
        layer surfaces them to the model."""
        from ee.agent.pocket_specialist import mcp_tool

        # The schema is constructed inline in build_pocket_specialist_server.
        # Read the source file directly — the embedded schema is the
        # easiest thing to grep for and we want a regression guard.
        src = mcp_tool.__file__
        with open(src, encoding="utf-8") as fh:
            text = fh.read()

        # Plan field NAMES present.
        for field in ("purpose", "layout", "focal_widget", "data_shape", "key_interactions"):
            assert f'"{field}"' in text, f"plan field {field!r} missing from MCP schema"

        # layout has the enum of valid shapes.
        for layout in (
            "hero+grid",
            "single-pane",
            "sidebar+main",
            "tabs",
            "master-detail",
            "stacked",
            "wizard",
        ):
            assert layout in text, f"layout {layout!r} missing from enum"


class TestParentPromptTeachesTwoPhase:
    def test_creation_prompt_mentions_two_phase_flow(self) -> None:
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        # Pre-delegation thinking block is the marquee change.
        assert "TWO-PHASE DELEGATION" in POCKET_CREATION_PROMPT_MCP
        assert "STEP 1 — UNDERSTAND THE BRIEF" in POCKET_CREATION_PROMPT_MCP
        assert "STEP 2 — PICK THE STRUCTURE" in POCKET_CREATION_PROMPT_MCP
        assert "STEP 3 — DELEGATE WITH A RICH PLAN" in POCKET_CREATION_PROMPT_MCP

    def test_creation_prompt_lists_layout_menu(self) -> None:
        """The parent must see the layout menu so it can pick."""
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        for layout in (
            "hero+grid",
            "single-pane",
            "sidebar+main",
            "tabs",
            "master-detail",
            "stacked",
            "wizard",
        ):
            assert layout in POCKET_CREATION_PROMPT_MCP

    def test_creation_prompt_shows_concrete_rich_hints_example(self) -> None:
        """A concrete example of a rich hints object — the model copies
        what it sees, so the example must show plan fields filled in."""
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        for field in ("layout", "focal_widget", "data_shape", "key_interactions"):
            assert f'"{field}"' in POCKET_CREATION_PROMPT_MCP

    def test_creation_prompt_caps_clarifying_questions(self) -> None:
        """Don't grill the user — 2 questions max."""
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        assert (
            "2 questions total" in POCKET_CREATION_PROMPT_MCP
            or "1–2" in POCKET_CREATION_PROMPT_MCP
            or "1-2" in POCKET_CREATION_PROMPT_MCP
        )

    def test_creation_prompt_keeps_bare_brief_backwards_compat(self) -> None:
        """A user with no plan must still be able to call __create
        with just `brief`."""
        from ee.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        assert "Backwards-compat" in POCKET_CREATION_PROMPT_MCP


class TestSpecialistPromptFollowsPlan:
    def test_specialist_prompt_says_follow_plan_dont_redesign(self) -> None:
        from ee.ripple._pockets import POCKET_SPECIALIST_PROMPT

        assert "FOLLOW THE PLAN" in POCKET_SPECIALIST_PROMPT
        assert (
            "AUTHORITATIVE" in POCKET_SPECIALIST_PROMPT
            or "authoritative" in POCKET_SPECIALIST_PROMPT
        )
        assert "faithful translation" in POCKET_SPECIALIST_PROMPT

    def test_specialist_prompt_lists_plan_fields_to_follow(self) -> None:
        from ee.ripple._pockets import POCKET_SPECIALIST_PROMPT

        for field in (
            "hints.layout",
            "hints.focal_widget",
            "hints.data_shape",
            "hints.key_interactions",
        ):
            assert field in POCKET_SPECIALIST_PROMPT
