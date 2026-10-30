# tests/cloud/test_ripple_required_props_gate.py
# Created: 2026-05-31 (constraint-zone enforcer) — EE-side wiring tests for
# the required-prop "HARD constraint" gate. The pure walker is covered in
# tests/test_ripple_required_props.py; this file covers the strict/logged
# variants, the agent-readable formatter, and the MissingRequiredPropError
# shape so the chat agent's retry loop can read it. Mirrors
# tests/cloud/test_ripple_validator_action_wiring.py.

from __future__ import annotations

import logging

import pytest
from pocketpaw_ee.cloud.ripple_validator import (
    MissingRequiredPropError,
    format_required_prop_violations_for_agent,
    validate_required_props_logged,
    validate_required_props_strict,
)

# `chart` requires `data`; `table` requires `columns` + `rows`; `text` requires
# nothing. Passed in the already-extracted map form (the shape `_gate_catalog`
# hands the validator from `_catalog_required_props`).
REQUIRED = {"chart": ["data"], "table": ["columns", "rows"]}


def _spec_missing_required() -> dict:
    """A chart node with no `data` — passes catalog + action-wiring, renders empty."""
    return {"ui": {"type": "chart", "props": {"title": "Sales"}}}


def _clean_spec() -> dict:
    return {"ui": {"type": "chart", "props": {"data": "{state.series}"}}}


# ---------------------------------------------------------------------------
# Strict mode raises MissingRequiredPropError
# ---------------------------------------------------------------------------


class TestStrict:
    def test_raises_on_missing_required_prop(self):
        with pytest.raises(MissingRequiredPropError) as ei:
            validate_required_props_strict(_spec_missing_required(), REQUIRED)
        # The exception carries the structured violations list AND formats a
        # human-readable message naming the missing prop.
        assert len(ei.value.violations) >= 1
        assert "data" in str(ei.value)
        assert ei.value.violations[0]["type"] == "chart"
        assert ei.value.violations[0]["missing"] == ["data"]

    def test_clean_spec_does_not_raise(self):
        # No assertion needed beyond "doesn't raise".
        validate_required_props_strict(_clean_spec(), REQUIRED)

    def test_no_required_map_does_not_raise(self):
        validate_required_props_strict(_spec_missing_required(), {})

    def test_multi_required_partial_raises_with_missing_only(self):
        spec = {"ui": {"type": "table", "props": {"columns": ["a"]}}}
        with pytest.raises(MissingRequiredPropError) as ei:
            validate_required_props_strict(spec, REQUIRED)
        assert ei.value.violations[0]["missing"] == ["rows"]


# ---------------------------------------------------------------------------
# Logged mode returns violations, never raises, emits structured warnings
# ---------------------------------------------------------------------------


class TestLogged:
    def test_returns_violations_without_raising(self):
        violations = validate_required_props_logged(_spec_missing_required(), REQUIRED)
        assert len(violations) == 1
        assert violations[0]["type"] == "chart"

    def test_clean_spec_returns_empty(self):
        assert validate_required_props_logged(_clean_spec(), REQUIRED) == []

    def test_emits_structured_warning_per_violation(self, caplog):
        with caplog.at_level(logging.WARNING):
            validate_required_props_logged(
                _spec_missing_required(),
                REQUIRED,
                pocket_id="pkt_1",
                workspace_id="ws_1",
            )
        records = [r for r in caplog.records if r.message == "ripple_spec.missing_required_prop"]
        assert len(records) == 1
        rec = records[0]
        assert rec.pocket_id == "pkt_1"
        assert rec.workspace_id == "ws_1"
        assert rec.widget_type == "chart"
        assert rec.missing_props == ["data"]

    def test_logged_does_not_block_on_violation(self):
        # The whole point of logged mode — an older imported spec with a
        # now-required prop missing is recorded for triage, not blocked.
        violations = validate_required_props_logged(_spec_missing_required(), REQUIRED)
        assert violations  # recorded
        # No exception escaped — reaching here is the assertion.


# ---------------------------------------------------------------------------
# Agent-readable formatter
# ---------------------------------------------------------------------------


class TestFormatter:
    def test_empty_violations_format_to_empty_string(self):
        assert format_required_prop_violations_for_agent([]) == ""

    def test_formatter_names_node_and_missing_prop(self):
        violations = [
            {"path": "ui.children[2]", "type": "chart", "missing": ["data"], "required": ["data"]}
        ]
        out = format_required_prop_violations_for_agent(violations)
        assert "ui.children[2]" in out
        assert "chart" in out
        assert "data" in out

    def test_formatter_caps_long_lists(self):
        violations = [
            {
                "path": f"ui.children[{i}]",
                "type": "chart",
                "missing": ["data"],
                "required": ["data"],
            }
            for i in range(15)
        ]
        out = format_required_prop_violations_for_agent(violations)
        assert "…and 5 more" in out

    def test_error_class_message_mirrors_siblings(self):
        # MissingRequiredPropError._format produces the same compact shape as
        # CatalogViolationError / ActionWiringViolationError.
        exc = MissingRequiredPropError(
            [{"path": "ui", "type": "stat", "missing": ["value"], "required": ["value"]}]
        )
        msg = str(exc)
        assert "required-prop violation" in msg
        assert "stat" in msg
        assert "value" in msg
