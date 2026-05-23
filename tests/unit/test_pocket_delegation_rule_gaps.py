# tests/unit/test_pocket_delegation_rule_gaps.py
# 2026-05-23 — Cover three system-prompt gaps surfaced by RFC 04/05
# end-to-end testing:
#   - inc 2a: STEP 0 template-library check was only in the heavy
#     creation_prompt, missing from POCKET_DELEGATION_RULE → MCP
#     backends never read the built-in templates.
#   - inc 2b follow-up: the live-data-sources guidance didn't make
#     "label-without-source = broken" explicit, so the specialist
#     wrote endpoint paths into widget labels but skipped the
#     ``set_source`` op that actually fetches.
#   - kanban-bind: the interactive-by-default block documented
#     ``bind:`` but didn't make node-level placement loud enough,
#     so kanban widgets rendered without a writeback path.
#
# These are content assertions, not behavior tests — the model still
# has to read and follow the rules. But each gap has a concrete
# regression case behind it, and a content drift is the easiest way
# to lose them again.

from __future__ import annotations

# Intentional private-attribute import: this file is a CONTENT regression
# guard for prompt blocks that are deliberately not part of
# ``pocketpaw.ripple``'s public surface. If a future refactor renames or
# relocates these constants, the test should fail loud (ImportError) so
# the new author sees the content rules need to move too — that is the
# whole point of pinning them at the source.
from pocketpaw.ripple._pockets import (
    _INTERACTIVE_DEFAULT_BLOCK,
    _LIVE_DATA_SOURCES_BLOCK,
    _LIVE_DATA_SOURCES_EDIT_BLOCK,
    POCKET_DELEGATION_RULE,
)


class TestTemplatePreflight:
    """STEP 0 must be wired into the delegation rule, not only the heavy
    creation prompt — MCP backends always read the delegation rule."""

    def test_template_preflight_section_present(self):
        assert "TEMPLATE PREFLIGHT" in POCKET_DELEGATION_RULE

    def test_template_preflight_runs_before_recipe(self):
        idx_template = POCKET_DELEGATION_RULE.find("TEMPLATE PREFLIGHT")
        idx_recipe = POCKET_DELEGATION_RULE.find("RECIPE PREFLIGHT")
        assert idx_template != -1 and idx_recipe != -1
        assert idx_template < idx_recipe, (
            "template preflight must precede recipe preflight in the rule"
        )

    def test_index_json_path_present(self):
        # The agent reads this file via Bash; the path must appear verbatim.
        assert "~/.pocketpaw/templates/index.json" in POCKET_DELEGATION_RULE

    def test_keyword_substring_match_rule(self):
        assert "case-insensitive SUBSTRING" in POCKET_DELEGATION_RULE

    def test_hints_doc_lists_template_id(self):
        # The hints schema doc must teach the chat agent that template_id
        # is a legitimate hint to pass — without it the agent would never
        # forward the preflight match to the specialist.
        assert "template_id" in POCKET_DELEGATION_RULE
        # And explicitly anchor it to the matched slug, not arbitrary text.
        assert "matched template" in POCKET_DELEGATION_RULE.lower() or (
            "the matched template" in POCKET_DELEGATION_RULE
            or "the matched" in POCKET_DELEGATION_RULE
        )

    def test_match_skips_recipe_preflight(self):
        # The rule must tell the agent to SKIP the recipe preflight on a
        # template match — otherwise both fire on every create, doubling
        # bash work and confusing the brief.
        # Whitespace-normalised so line-wrapping doesn't break the assert.
        flat = " ".join(POCKET_DELEGATION_RULE.split())
        assert "SKIP the recipe preflight" in flat


class TestLabelWithoutSourceRule:
    """The specialist authored ``state.pets``-bound widgets and labels
    like "Live from /pet/findByStatus" but skipped ``set_source``. The
    label/source pairing rule must be loud in both the create-variant
    block (whole-spec authoring) and the edit-variant block (granular
    ops via set_source)."""

    def test_create_block_pairs_label_with_source(self):
        assert "label without source" in _LIVE_DATA_SOURCES_BLOCK.lower()

    def test_edit_block_pairs_label_with_set_source(self):
        assert "label without `set_source`" in _LIVE_DATA_SOURCES_EDIT_BLOCK or (
            "label without ``set_source``" in _LIVE_DATA_SOURCES_EDIT_BLOCK
        )

    def test_create_block_names_the_three_required_pieces(self):
        # source entry + state seed + widget — all three must be named
        # as the unit so the LLM doesn't ship two-of-three.
        assert "bind" in _LIVE_DATA_SOURCES_BLOCK
        assert "state" in _LIVE_DATA_SOURCES_BLOCK
        assert "refresh" in _LIVE_DATA_SOURCES_BLOCK

    def test_edit_block_names_set_source_set_state_add_node(self):
        # The edit specialist uses granular ops; all three op names must
        # appear in the pairing rule.
        assert "set_source" in _LIVE_DATA_SOURCES_EDIT_BLOCK
        assert "set_state" in _LIVE_DATA_SOURCES_EDIT_BLOCK
        assert "add_node" in _LIVE_DATA_SOURCES_EDIT_BLOCK


class TestInteractiveBindRule:
    """Kanban (and other stateful interactive widgets) rendered without
    writeback because the spec carried ``props.value: "{state.tasks}"``
    instead of ``node.bind: "state.tasks"``. The interactive-by-default
    block must call this out explicitly."""

    def test_block_calls_out_node_level_bind(self):
        assert "node level" in _INTERACTIVE_DEFAULT_BLOCK.lower() or (
            "NODE LEVEL" in _INTERACTIVE_DEFAULT_BLOCK
        )

    def test_block_carries_wrong_and_right_examples(self):
        # The contrasting examples are what stop the model from nesting
        # bind inside props — a rule without the visual diff doesn't
        # land.
        assert "WRONG" in _INTERACTIVE_DEFAULT_BLOCK
        assert "RIGHT" in _INTERACTIVE_DEFAULT_BLOCK

    def test_block_enumerates_widgets_that_need_node_bind(self):
        # The list anchors the rule to the actual catalog — if the model
        # doesn't recognize the widget, it won't apply the rule.
        for widget_type in ("kanban", "calendar", "checkbox", "input", "select"):
            assert widget_type in _INTERACTIVE_DEFAULT_BLOCK, (
                f"{widget_type} missing from the interactive-bind enumeration"
            )

    def test_block_says_read_only_widgets_dont_need_bind(self):
        # Without this caveat the model over-applies ``bind:`` to
        # display-only widgets, which the renderer warns about and
        # which clutters specs.
        block_lower = _INTERACTIVE_DEFAULT_BLOCK.lower()
        assert "read-only" in block_lower or "does not need" in block_lower
