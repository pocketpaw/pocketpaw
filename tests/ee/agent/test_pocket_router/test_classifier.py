# test_classifier.py — The safety-net corpus test for the pure tier
#   classifier (Increment 3).
# Created: 2026-05-22 — the classifier decides whether to SKIP the LLM
#   specialist for an edit. A wrong Tier-0/1 verdict produces a broken
#   pocket, so this corpus is deliberately exhaustive: every labelled
#   intent asserts its expected tier, and the fail-safe escalations
#   (partial match, structural verb, ambiguous target, unresolved
#   action params, requires_instinct) each get their own dedicated case.
"""Labelled-corpus safety net for the pocket-router classifier."""

from __future__ import annotations

import pytest
from pocketpaw_ee.agent.pocket_router.classifier import classify

# ---------------------------------------------------------------------------
# Fixture rippleSpecs
# ---------------------------------------------------------------------------

# A pocket with one declared GET source and one declared write action.
_SPEC_WITH_BINDINGS = {
    "version": "1.0",
    "sources": {
        "prs": {"method": "GET", "path": "/pulls", "bind": "state.prs"},
    },
    "actions": {
        "publish": {
            "kind": "write_binding",
            "method": "POST",
            "path": "/publish",
            "params": {"channel": "release"},  # static — resolves
        },
    },
    "state": {"prs": [], "filter": "all"},
    "ui": {"id": "n_root0000", "type": "flex", "props": {}, "children": []},
}

# A task pocket: one indexable task collection in state, a heading node.
_SPEC_TASKS = {
    "version": "1.0",
    "state": {
        "tasks": [
            {"id": 1, "label": "buy milk", "status": "todo"},
            {"id": 2, "label": "walk dog", "status": "todo"},
            {"id": 3, "label": "write tests", "status": "todo"},
        ],
        "filter": "all",
    },
    "ui": {
        "id": "n_root0000",
        "type": "flex",
        "props": {},
        "children": [
            {"id": "n_head0000", "type": "heading", "props": {"text": "My Tasks"}},
        ],
    },
}

# An action whose params carry an unresolved Ripple expression.
_SPEC_UNRESOLVED_ACTION = {
    "version": "1.0",
    "actions": {
        "submit": {
            "kind": "write_binding",
            "method": "POST",
            "path": "/submit",
            "params": {"taskId": "{item.id}"},  # unresolved — must escalate
        },
    },
    "state": {},
    "ui": {"id": "n_root0000", "type": "flex", "props": {}, "children": []},
}

# An action gated by Instinct — must NOT be auto-fired by the router.
_SPEC_INSTINCT_ACTION = {
    "version": "1.0",
    "actions": {
        "publish": {
            "kind": "write_binding",
            "method": "POST",
            "path": "/publish",
            "params": {},
            "requires_instinct": True,
        },
    },
    "state": {},
    "ui": {"id": "n_root0000", "type": "flex", "props": {}, "children": []},
}

# Two declared sources — a refresh that names neither is ambiguous.
_SPEC_TWO_SOURCES = {
    "version": "1.0",
    "sources": {
        "orders": {"method": "GET", "path": "/orders", "bind": "state.orders"},
        "customers": {"method": "GET", "path": "/customers", "bind": "state.customers"},
    },
    "state": {"orders": [], "customers": []},
    "ui": {"id": "n_root0000", "type": "flex", "props": {}, "children": []},
}


# ---------------------------------------------------------------------------
# Tier 0 — declarative
# ---------------------------------------------------------------------------


class TestTier0Declarative:
    """A clean 1:1 refresh/action against a declared binding -> Tier 0."""

    @pytest.mark.parametrize(
        "intent",
        [
            "refresh the prs source",
            "reload prs",
            "sync prs",
            "fetch prs",
            "pull prs",
        ],
    )
    def test_refresh_named_source_is_tier0(self, intent):
        c = classify(intent, _SPEC_WITH_BINDINGS)
        assert c.tier == 0, f"{intent!r} -> {c.tier} ({c.reasoning})"
        assert c.op == "run_source"
        assert c.target == "prs"
        assert c.op_args == {"source": "prs"}
        assert c.confidence >= 0.9

    @pytest.mark.parametrize("intent", ["submit the publish action", "save publish"])
    def test_submit_named_action_is_tier0(self, intent):
        c = classify(intent, _SPEC_WITH_BINDINGS)
        assert c.tier == 0, f"{intent!r} -> {c.tier} ({c.reasoning})"
        assert c.op == "run_action"
        assert c.target == "publish"

    def test_refresh_with_no_named_source_escalates(self):
        # "refresh the dashboard" names no source — creative, not declarative.
        c = classify("refresh the dashboard", _SPEC_WITH_BINDINGS)
        assert c.tier == 2

    def test_refresh_unknown_source_escalates(self):
        c = classify("refresh the invoices source", _SPEC_WITH_BINDINGS)
        assert c.tier == 2

    def test_refresh_ambiguous_two_sources_escalates(self):
        # "refresh everything" matches no key; but a refresh that could
        # touch multiple sources must never be auto-routed.
        c = classify("sync the data", _SPEC_TWO_SOURCES)
        assert c.tier == 2


# ---------------------------------------------------------------------------
# Tier 1 — deterministic single op
# ---------------------------------------------------------------------------


class TestTier1Deterministic:
    """An unambiguous single-op intent -> Tier 1."""

    @pytest.mark.parametrize(
        ("intent", "expected_status"),
        [
            ("mark task 1 as done", "done"),
            ("mark task 2 done", "done"),
            ("complete task 3", "done"),
            ("check task 1", "done"),
            ("mark task 1 as todo", "todo"),
            ("mark task 2 as open", "todo"),
        ],
    )
    def test_mark_task_done_is_tier1_set_state(self, intent, expected_status):
        c = classify(intent, _SPEC_TASKS)
        assert c.tier == 1, f"{intent!r} -> {c.tier} ({c.reasoning})"
        assert c.op == "set_state"
        assert c.op_args["value"] == expected_status
        assert c.op_args["path"].startswith("tasks[")
        assert c.op_args["path"].endswith(".status")

    def test_mark_task_index_is_one_based(self):
        # "task 1" -> tasks[0]; "task 3" -> tasks[2].
        assert classify("mark task 1 done", _SPEC_TASKS).op_args["path"] == "tasks[0].status"
        assert classify("mark task 3 done", _SPEC_TASKS).op_args["path"] == "tasks[2].status"

    def test_set_filter_is_tier1_set_state(self):
        c = classify("set the filter to overdue", _SPEC_TASKS)
        assert c.tier == 1
        assert c.op == "set_state"
        assert c.op_args == {"path": "filter", "value": "overdue"}

    def test_rename_single_node_is_tier1_set_node_prop(self):
        c = classify('rename My Tasks to "Today\'s Work"', _SPEC_TASKS)
        assert c.tier == 1, f"-> {c.tier} ({c.reasoning})"
        assert c.op == "set_node_prop"
        assert c.target == "n_head0000"
        assert c.op_args["prop"] == "text"
        assert c.op_args["value"] == "Today's Work"


# ---------------------------------------------------------------------------
# Tier 2 — specialist / escalation
# ---------------------------------------------------------------------------


class TestTier2Escalation:
    """Structural / creative / ambiguous intents -> Tier 2."""

    @pytest.mark.parametrize(
        "intent",
        [
            "add a chart widget",
            "add a status badge to the header",
            "create a new section for metrics",
            "insert a divider",
            "delete the chart",
            "move the heading below the table",
            "rebuild this as a kanban board",
            "redesign the layout",
            "restructure the dashboard",
            "split the table into two",
            "convert the list to a grid",
            "wrap the cards in a flex container",
            "group the stats together",
            "duplicate the chart",
        ],
    )
    def test_structural_verbs_escalate(self, intent):
        c = classify(intent, _SPEC_TASKS)
        assert c.tier == 2, f"{intent!r} -> {c.tier} ({c.reasoning})"
        assert c.is_escalation

    @pytest.mark.parametrize(
        "intent",
        [
            "make this less cluttered",
            "clean up the layout",
            "improve the design",
            "polish the dashboard",
            "simplify this",
            "modernize the look",
            "fix the spacing",
            "make it nicer",
        ],
    )
    def test_creative_verbs_escalate(self, intent):
        c = classify(intent, _SPEC_TASKS)
        assert c.tier == 2, f"{intent!r} -> {c.tier} ({c.reasoning})"

    def test_ambiguous_intent_escalates(self):
        c = classify("do the thing with the stuff", _SPEC_TASKS)
        assert c.tier == 2

    def test_empty_intent_escalates(self):
        assert classify("", _SPEC_TASKS).tier == 2
        assert classify("  ", _SPEC_TASKS).tier == 2
        assert classify("hi", _SPEC_TASKS).tier == 2

    # ---- partial-match escalations: a tier verb is present but the
    # object cannot be pinned to exactly one spec entity ----

    def test_partial_mark_no_index_escalates(self):
        # "mark the tasks done" — a mark verb, but no single numeric target.
        c = classify("mark the tasks done", _SPEC_TASKS)
        assert c.tier == 2

    def test_partial_mark_index_out_of_range_escalates(self):
        # task 9 does not exist in a 3-task collection.
        c = classify("mark task 9 done", _SPEC_TASKS)
        assert c.tier == 2

    def test_partial_mark_ambiguous_direction_escalates(self):
        # "done" AND "todo" both present — ambiguous direction.
        c = classify("mark task 1 done or todo", _SPEC_TASKS)
        assert c.tier == 2

    def test_mark_with_no_indexable_collection_escalates(self):
        # state has no list-of-objects to address.
        spec = {"state": {"count": 5, "filter": "all"}, "ui": {"id": "n_r", "type": "flex"}}
        c = classify("mark task 1 done", spec)
        assert c.tier == 2

    def test_mark_with_two_collections_escalates(self):
        # two candidate lists -> the index is ambiguous.
        spec = {
            "state": {
                "tasks": [{"id": 1}],
                "bugs": [{"id": 1}],
            },
            "ui": {"id": "n_r", "type": "flex"},
        }
        c = classify("mark task 1 done", spec)
        assert c.tier == 2

    def test_rename_ambiguous_two_nodes_escalates(self):
        # two headings both reading "Status" — rename can't pick one.
        spec = {
            "state": {},
            "ui": {
                "id": "n_root",
                "type": "flex",
                "props": {},
                "children": [
                    {"id": "n_a", "type": "heading", "props": {"text": "Status"}},
                    {"id": "n_b", "type": "heading", "props": {"text": "Status"}},
                ],
            },
        }
        c = classify('rename Status to "State"', spec)
        assert c.tier == 2

    def test_rename_no_matching_node_escalates(self):
        c = classify('rename Nonexistent to "X"', _SPEC_TASKS)
        assert c.tier == 2

    def test_rename_without_to_clause_escalates(self):
        c = classify("rename the heading", _SPEC_TASKS)
        assert c.tier == 2

    def test_set_filter_without_scalar_field_escalates(self):
        spec = {"state": {"tasks": []}, "ui": {"id": "n_r", "type": "flex"}}
        c = classify("set the filter to overdue", spec)
        assert c.tier == 2

    # ---- action-binding escalations ----

    def test_action_with_unresolved_params_escalates(self):
        # publish/submit action whose params carry {item.id}.
        c = classify("submit the submit action", _SPEC_UNRESOLVED_ACTION)
        assert c.tier == 2, f"-> {c.tier} ({c.reasoning})"

    def test_requires_instinct_action_escalates(self):
        # an Instinct-gated action must never be auto-fired.
        c = classify("submit the publish action", _SPEC_INSTINCT_ACTION)
        assert c.tier == 2, f"-> {c.tier} ({c.reasoning})"


# ---------------------------------------------------------------------------
# Purity — the classifier never mutates its inputs.
# ---------------------------------------------------------------------------


def test_classifier_does_not_mutate_ripple_spec():
    import copy

    spec = copy.deepcopy(_SPEC_TASKS)
    before = copy.deepcopy(spec)
    for intent in ["mark task 1 done", "add a chart", "refresh prs", "make it nicer"]:
        classify(intent, spec)
    assert spec == before, "classify() mutated its ripple_spec argument"


def test_classification_is_frozen():
    c = classify("add a widget", _SPEC_TASKS)
    with pytest.raises((AttributeError, TypeError)):
        c.tier = 0  # type: ignore[misc]
