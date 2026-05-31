"""Tests for the new widget-diversity / data-value-ask rules.

Captures the prompt changes that prevent two failure modes seen in
production:

  * Agent invents placeholder data (octocat) when user asks for "my
    GitHub account" without naming a username.
  * Agent emits 4+ tables when audit-log / timeline / kanban /
    tree-table would each fit one of the lists better.
"""

from __future__ import annotations


class TestAskForMissingDataValues:
    def test_creation_prompt_distinguishes_data_vs_structure_questions(self) -> None:
        """The parent prompt must split clarifying questions into two
        categories: missing DATA VALUES (handle, names, dates) and
        STRUCTURAL ambiguity."""
        from pocketpaw.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        assert "MISSING DATA VALUES" in POCKET_CREATION_PROMPT_MCP
        assert "STRUCTURAL" in POCKET_CREATION_PROMPT_MCP

    def test_creation_prompt_calls_out_github_username_example(self) -> None:
        """Concrete examples teach the agent to look for missing
        identity fields. The GitHub case is the one that bit us."""
        from pocketpaw.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        assert "github" in POCKET_CREATION_PROMPT_MCP.lower()
        assert "username" in POCKET_CREATION_PROMPT_MCP.lower()

    def test_creation_prompt_bans_placeholder_data(self) -> None:
        """No more octocat / acme corp. The prompt must explicitly
        forbid placeholder names."""
        from pocketpaw.ripple._pockets import POCKET_CREATION_PROMPT_MCP

        assert "octocat" in POCKET_CREATION_PROMPT_MCP.lower()
        assert "Never invent placeholder" in POCKET_CREATION_PROMPT_MCP


class TestActivityWidgetsInFreeList:
    def test_audit_log_and_timeline_in_free_list(self) -> None:
        """Both widgets are now no-tool-call to emit."""
        from pocketpaw.ripple._design import WIDGET_SPEC_TOOL_RULE

        # The FREE LIST is a section within WIDGET_SPEC_TOOL_RULE.
        assert "audit-log" in WIDGET_SPEC_TOOL_RULE
        assert "timeline" in WIDGET_SPEC_TOOL_RULE

    def test_canonical_shapes_inline_audit_log_props(self) -> None:
        from pocketpaw.ripple._design import CANONICAL_SHAPES

        assert "`audit-log`" in CANONICAL_SHAPES
        # The example uses the manifest's actual prop shape.
        assert '"entries"' in CANONICAL_SHAPES
        assert "actor" in CANONICAL_SHAPES
        assert "groupBy" in CANONICAL_SHAPES

    def test_canonical_shapes_inline_timeline_props(self) -> None:
        from pocketpaw.ripple._design import CANONICAL_SHAPES

        assert "`timeline`" in CANONICAL_SHAPES
        assert '"events"' in CANONICAL_SHAPES
        # Manifest's event shape.
        assert '"date"' in CANONICAL_SHAPES or "date, title" in CANONICAL_SHAPES


class TestActivityPickerRule:
    def test_activity_picker_rule_exported(self) -> None:
        from pocketpaw.ripple._design import ACTIVITY_PICKER_RULE

        assert "ACTIVITY PICKER" in ACTIVITY_PICKER_RULE
        assert "audit-log" in ACTIVITY_PICKER_RULE
        assert "timeline" in ACTIVITY_PICKER_RULE

    def test_activity_picker_explicitly_forbids_table(self) -> None:
        from pocketpaw.ripple._design import ACTIVITY_PICKER_RULE

        assert "NEVER a `table`" in ACTIVITY_PICKER_RULE or "NEVER a table" in ACTIVITY_PICKER_RULE

    def test_activity_picker_in_ripple_design_rules(self) -> None:
        """The block must be wired into the assembled prompt the
        specialist actually reads."""
        from pocketpaw.ripple._design import RIPPLE_DESIGN_RULES

        assert "ACTIVITY PICKER" in RIPPLE_DESIGN_RULES


class TestTableCap:
    def test_visual_variation_caps_tables(self) -> None:
        from pocketpaw.ripple._design import VISUAL_VARIATION_RULE

        # Some phrasing of the cap.
        cap_phrases = (
            "NO TABLE-STAMPEDES",
            "at most",
            "2 `table`",
        )
        assert any(p in VISUAL_VARIATION_RULE for p in cap_phrases)

    def test_visual_variation_redirects_to_typed_widgets(self) -> None:
        """When you have 3+ lists, the cap must point at concrete
        alternatives (audit-log / timeline / kanban / tree-table / etc.)."""
        from pocketpaw.ripple._design import VISUAL_VARIATION_RULE

        for alternative in ("audit-log", "timeline", "kanban", "tree-table"):
            assert alternative in VISUAL_VARIATION_RULE
