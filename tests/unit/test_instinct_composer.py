# tests/unit/test_instinct_composer.py
# Created: 2026-05-28 (feat/rfc-03-v2-instinct-exec) — unit tests for
# the 5-step Instinct resolution composer
# (``instinct_composer.resolve_instinct``). Pins the RFC 03 v2
# resolution order, the ``block always wins`` invariant, and the
# ``operator overlay can only escalate`` invariant. Worked examples A,
# B, C from the RFC are pinned against the bundled
# ``lease-renewal-v2.yaml`` fixture; synthetic templates cover edge
# cases (no instinct_rules, action lookup miss, CEL eval failure).
"""Tests for the Instinct 5-step resolution composer (RFC 03 v2 PR 2d).

The composer is a pure library function: given a template, an action
name, a row context, and (optionally) a workspace context, it returns
a frozen ``InstinctDecision`` describing what the EE runtime should do
next — BLOCK, ESCALATE_APPROVAL, EXECUTE, or NOTIFY_AND_EXECUTE.

These tests pin:

* The three RFC worked examples (A, B, C) end-to-end against the real
  ``lease-renewal-v2.yaml`` fixture so the composer stays honest to
  the spec's narrative.
* The two RFC invariants:
    - ``block always wins`` — a matching block rule short-circuits steps
      2-5 even when an approval rule would also match.
    - ``operator overlay can only escalate`` — top-level approval rules
      can promote ``auto`` to ``ESCALATE_APPROVAL`` but cannot demote a
      per-action ``require_approval`` floor.
* Edge cases — missing action, CEL eval failure, empty / absent
  ``instinct_rules``, notify rule capture, ``notify_only`` policy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from pocketpaw.bundled_templates import (
    InstinctDecision,
    InstinctResolutionError,
    PocketTemplate,
    TemplateIdentifierResolver,
    resolve_instinct,
)

# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "templates" / "lease-renewal-v2.yaml"
)

# Fixed clock so ``within(...)`` is deterministic across the suite.
FROZEN_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _load_lease_template() -> PocketTemplate:
    """Load the canonical worked-example fixture."""
    raw = yaml.safe_load(FIXTURE_PATH.read_text())
    return PocketTemplate.model_validate(raw)


def _row_with_all_rule_fields(**overrides: Any) -> dict[str, Any]:
    """Return a row dict that satisfies every identifier referenced by
    the fixture's ``instinct_rules.rules[]``. Tests override the bits
    they care about.

    The fixture's four rules reference ``rent_proposed``,
    ``rent_current``, ``rent_proposed_delta_pct``,
    ``tenant.late_payment_count_12mo``, ``expires_at``, and
    ``renewal_stage``. Without every one of these present, step 2 (or
    even step 1) will trip ``CelEvaluationError`` on a missing
    identifier — that is the resolver's contract, not a composer bug.
    """
    base: dict[str, Any] = {
        "rent_current": 2000.0,
        "rent_proposed": 2050.0,
        "rent_proposed_delta_pct": 2.5,
        "renewal_stage": "draft",
        "days_remaining": 60,
        "expires_at": datetime(2026, 12, 1, tzinfo=UTC),
        "tenant": {"late_payment_count_12mo": 0},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# RFC worked examples — A, B, C
# ---------------------------------------------------------------------------


def test_worked_example_a_block_wins_over_auto() -> None:
    """Example A — auto action ``mark_renewed`` blocked by top-level
    rule ``rent_proposed < rent_current * 0.95``.

    NOTE: the bundled fixture declares ``mark_renewed`` as
    ``notify_only`` (not ``auto`` as in the RFC narrative). The block
    rule fires in step 1 regardless of the per-action floor; this is
    the ``block always wins`` invariant.
    """
    template = _load_lease_template()
    row = _row_with_all_rule_fields(rent_proposed=1800.0, rent_current=2000.0)
    decision = resolve_instinct(template, "mark_renewed", row, now=FROZEN_NOW)

    assert decision.verdict == "BLOCK"
    assert decision.reason == "blocked_by_rule"
    assert decision.action_name == "mark_renewed"
    assert len(decision.matched_rules) == 1
    assert decision.matched_rules[0].action == "block"
    # Side effects are not collected on BLOCK — step 1 short-circuits
    # all later steps including notify gathering.
    assert decision.notify_rules == []


def test_worked_example_b_operator_overlay_escalates_auto() -> None:
    """Example B — ``send_to_tenant`` declared ``require_approval`` in
    the fixture; using a synthetic template that flips it to ``auto``
    so the operator-overlay-escalates path is exercised independently
    of the per-action floor."""
    template = _load_lease_template()
    # The fixture's send_to_tenant is require_approval (which is a
    # FLOOR, not the path we want to exercise here). Mutate the
    # in-memory model copy to auto so we exercise the overlay-promotes
    # path cleanly.
    actions = list(template.actions)
    sti = next(i for i, a in enumerate(actions) if a.name == "send_to_tenant")
    actions[sti] = actions[sti].model_copy(update={"instinct_policy": "auto"})
    template = template.model_copy(update={"actions": actions})

    row = _row_with_all_rule_fields(tenant={"late_payment_count_12mo": 4})
    decision = resolve_instinct(template, "send_to_tenant", row, now=FROZEN_NOW)

    assert decision.verdict == "ESCALATE_APPROVAL"
    assert decision.reason == "operator_overlay_escalated"
    # The matched rule is the second require_approval rule on the
    # fixture (the first one is the rent-delta one which row B does
    # not trigger).
    assert any("late_payment_count_12mo" in r.when for r in decision.matched_rules), (
        f"expected late-payment rule in matched_rules, got {decision.matched_rules}"
    )


def test_worked_example_c_require_approval_floor() -> None:
    """Example C — ``bulk_draft`` declares ``instinct_policy:
    require_approval``. No top-level rule matches. The per-action
    floor escalates."""
    template = _load_lease_template()
    row = _row_with_all_rule_fields(
        rent_proposed=2050.0,
        rent_current=2000.0,
        rent_proposed_delta_pct=2.5,
        renewal_stage=None,
        tenant={"late_payment_count_12mo": 1},
    )
    decision = resolve_instinct(template, "bulk_draft", row, now=FROZEN_NOW)

    assert decision.verdict == "ESCALATE_APPROVAL"
    assert decision.reason == "author_floor"
    # No rule participated — step 2 matched nothing.
    assert decision.matched_rules == []


# ---------------------------------------------------------------------------
# Per-action policy paths
# ---------------------------------------------------------------------------


def test_auto_action_no_rules_match_executes() -> None:
    """``draft_renewal`` is auto. With a clean row, no rules match →
    EXECUTE, reason ``auto``."""
    template = _load_lease_template()
    row = _row_with_all_rule_fields()
    decision = resolve_instinct(template, "draft_renewal", row, now=FROZEN_NOW)

    assert decision.verdict == "EXECUTE"
    assert decision.reason == "auto"
    assert decision.matched_rules == []


def test_notify_only_policy_emits_notify_and_execute() -> None:
    """``mark_renewed`` is notify_only. With a clean row (no block rule
    matches, no approval rule matches) → step 3 picks notify_only →
    verdict NOTIFY_AND_EXECUTE."""
    template = _load_lease_template()
    row = _row_with_all_rule_fields()
    decision = resolve_instinct(template, "mark_renewed", row, now=FROZEN_NOW)

    assert decision.verdict == "NOTIFY_AND_EXECUTE"
    assert decision.reason == "notify_only"


def test_notify_rule_matches_alongside_execute() -> None:
    """A row that triggers the fixture's notify rule (within 7d of
    expiry AND renewal_stage == null) and uses an auto action should
    EXECUTE with notify_rules captured. Block / approval rules do not
    match this row."""
    template = _load_lease_template()
    # within(expires_at, 7d) at FROZEN_NOW => expires_at must be inside
    # 2026-05-21..2026-06-04. Pick a stage-null row.
    row = _row_with_all_rule_fields(
        expires_at=datetime(2026, 5, 30, tzinfo=UTC),
        renewal_stage=None,
        rent_proposed=2050.0,
        rent_current=2000.0,
        rent_proposed_delta_pct=2.5,
        tenant={"late_payment_count_12mo": 0},
    )
    decision = resolve_instinct(template, "draft_renewal", row, now=FROZEN_NOW)

    assert decision.verdict == "EXECUTE"
    assert decision.reason == "auto"
    # The notify rule on the fixture is rule index 3:
    #   when: "within(expires_at, duration('7d')) && renewal_stage == null"
    assert any(r.action == "notify" for r in decision.notify_rules), (
        f"expected at least one notify rule, got {decision.notify_rules}"
    )


# ---------------------------------------------------------------------------
# RFC invariants
# ---------------------------------------------------------------------------


def test_block_always_wins_over_simultaneous_approval_rule() -> None:
    """Row that matches BOTH the block rule AND an approval rule must
    still return BLOCK. Step 1 short-circuits all later steps."""
    template = _load_lease_template()
    # rent_proposed < rent_current * 0.95 matches (block) AND
    # tenant.late_payment_count_12mo >= 3 matches (approval).
    row = _row_with_all_rule_fields(
        rent_proposed=1800.0,
        rent_current=2000.0,
        tenant={"late_payment_count_12mo": 5},
    )
    decision = resolve_instinct(template, "draft_renewal", row, now=FROZEN_NOW)

    assert decision.verdict == "BLOCK"
    assert decision.reason == "blocked_by_rule"
    # Only the block rule participates — approval rule must not show up
    # in matched_rules because step 2 never ran.
    assert all(r.action == "block" for r in decision.matched_rules)


def test_operator_overlay_cannot_demote_require_approval_floor() -> None:
    """``require_approval`` per-action policy with a clean row that
    matches no top-level rule must still ESCALATE — the overlay never
    runs (no match), so the per-action floor fires in step 3.

    This pins the half of the invariant that says ``the overlay can
    only escalate, never demote``. If a no-op step-2 silently
    short-circuited to EXECUTE, this test would fail.
    """
    template = _load_lease_template()
    row = _row_with_all_rule_fields()
    decision = resolve_instinct(template, "send_to_tenant", row, now=FROZEN_NOW)

    assert decision.verdict == "ESCALATE_APPROVAL"
    assert decision.reason == "author_floor"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unknown_action_raises_resolution_error() -> None:
    template = _load_lease_template()
    row = _row_with_all_rule_fields()
    with pytest.raises(InstinctResolutionError) as excinfo:
        resolve_instinct(template, "does_not_exist", row, now=FROZEN_NOW)
    assert "does_not_exist" in str(excinfo.value)


def test_cel_eval_failure_in_rule_raises_resolution_error() -> None:
    """Missing identifier in a rule's ``when`` -> the underlying
    ``CelEvaluationError`` is wrapped in ``InstinctResolutionError``
    so callers have a single typed exception to handle."""
    template = _load_lease_template()
    # Omit ``rent_proposed_delta_pct`` so the first approval rule
    # fails to resolve at eval time.
    row = {
        "rent_current": 2000.0,
        "rent_proposed": 2050.0,
        "renewal_stage": "draft",
        "expires_at": datetime(2026, 12, 1, tzinfo=UTC),
        "tenant": {"late_payment_count_12mo": 0},
    }
    with pytest.raises(InstinctResolutionError) as excinfo:
        resolve_instinct(template, "draft_renewal", row, now=FROZEN_NOW)
    # The original CelEvaluationError should be reachable.
    assert excinfo.value.__cause__ is not None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def _minimal_template(**overrides: Any) -> PocketTemplate:
    """Synthetic minimal template — no joined entities, single auto
    action, optional instinct_rules override."""
    data: dict[str, Any] = {
        "schema_version": "2",
        "name": "min-template",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "test",
        "display_name": "Minimal",
        "description": "Minimal template for composer tests.",
        "shape": "data-grid",
        "state": {
            "entity_type": "Thing",
            "id_field": "id",
            "columns": [{"field": "id", "widget": "text"}, {"field": "flag", "widget": "text"}],
        },
        "actions": [
            {"name": "go", "label": "Go", "kind": "single-row", "instinct_policy": "auto"},
            {
                "name": "require_go",
                "label": "Require go",
                "kind": "single-row",
                "instinct_policy": "require_approval",
            },
            {
                "name": "notify_go",
                "label": "Notify go",
                "kind": "single-row",
                "instinct_policy": "notify_only",
            },
        ],
    }
    data.update(overrides)
    return PocketTemplate.model_validate(data)


def test_template_without_instinct_rules_flows_to_per_action_policy() -> None:
    """When ``instinct_rules`` is None, steps 1 and 2 are skipped and
    the decision is driven entirely by the per-action policy."""
    template = _minimal_template()
    # No row fields needed — no rules to evaluate.
    decision = resolve_instinct(template, "go", {}, now=FROZEN_NOW)
    assert decision.verdict == "EXECUTE"
    assert decision.reason == "auto"

    decision_req = resolve_instinct(template, "require_go", {}, now=FROZEN_NOW)
    assert decision_req.verdict == "ESCALATE_APPROVAL"
    assert decision_req.reason == "author_floor"

    decision_notify = resolve_instinct(template, "notify_go", {}, now=FROZEN_NOW)
    assert decision_notify.verdict == "NOTIFY_AND_EXECUTE"
    assert decision_notify.reason == "notify_only"


def test_template_with_empty_instinct_rules_list_behaves_like_none() -> None:
    """``instinct_rules: { rules: [] }`` is the explicit-empty case —
    same observable behaviour as the absent case."""
    template = _minimal_template(
        instinct_rules={"escalation": "lead", "rules": []},
    )
    decision = resolve_instinct(template, "go", {}, now=FROZEN_NOW)
    assert decision.verdict == "EXECUTE"
    assert decision.reason == "auto"


def test_bulk_action_decision_shape_matches_single_row() -> None:
    """``kind: bulk`` does not change the composer's shape — PR 2e
    handles fan-out; PR 2d just answers ``what would happen for this
    row``."""
    template = _load_lease_template()
    row = _row_with_all_rule_fields(
        rent_proposed=2050.0,
        rent_current=2000.0,
        rent_proposed_delta_pct=2.5,
        renewal_stage="draft",
        tenant={"late_payment_count_12mo": 1},
    )
    decision = resolve_instinct(template, "bulk_draft", row, now=FROZEN_NOW)

    assert decision.verdict == "ESCALATE_APPROVAL"
    assert decision.reason == "author_floor"
    assert decision.action_name == "bulk_draft"


def test_workspace_context_merge_row_wins_on_collision() -> None:
    """Workspace context is merged in; row context wins on a collision
    so per-row data is never silently shadowed by workspace defaults."""
    template = _minimal_template(
        instinct_rules={
            "escalation": "lead",
            "rules": [{"when": "flag == 'block_me'", "action": "block"}],
        },
    )

    # workspace says flag='ok'; row overrides flag='block_me'.
    decision = resolve_instinct(
        template,
        "go",
        {"flag": "block_me"},
        workspace_context={"flag": "ok"},
        now=FROZEN_NOW,
    )
    assert decision.verdict == "BLOCK"

    # workspace says block; row says ok → row wins, no block.
    decision_ok = resolve_instinct(
        template,
        "go",
        {"flag": "ok"},
        workspace_context={"flag": "block_me"},
        now=FROZEN_NOW,
    )
    assert decision_ok.verdict == "EXECUTE"


def test_decision_is_frozen() -> None:
    """``InstinctDecision`` is immutable — callers cannot mutate fields
    in place. Pins the design choice in the brief."""
    template = _minimal_template()
    decision = resolve_instinct(template, "go", {}, now=FROZEN_NOW)
    assert isinstance(decision, InstinctDecision)
    with pytest.raises((TypeError, ValueError)):
        decision.verdict = "BLOCK"  # type: ignore[misc]


def test_custom_resolver_can_be_injected() -> None:
    """Callers can override the default resolver (e.g. PR 2g's future
    Fabric resolver). Pin by passing a stub that returns a constant."""
    sentinel = "stub"

    class _StubResolver:
        def resolve(self, path: str, context: dict[str, Any]) -> Any:  # noqa: ARG002
            # Return ``sentinel`` for any identifier — the test
            # expression ``flag == 'stub'`` becomes True.
            return sentinel

    template_with_rule = _minimal_template(
        instinct_rules={
            "escalation": "lead",
            "rules": [{"when": "flag == 'stub'", "action": "require_approval"}],
        },
    )
    decision = resolve_instinct(
        template_with_rule,
        "go",
        {},
        resolver=_StubResolver(),
        now=FROZEN_NOW,
    )
    assert decision.verdict == "ESCALATE_APPROVAL"
    assert decision.reason == "operator_overlay_escalated"


def test_default_resolver_used_when_none_passed() -> None:
    """If ``resolver`` is None, the composer constructs a default
    ``TemplateIdentifierResolver(template.state)``. Pin by exercising
    a row that depends on the resolver's column-aware logic."""
    template = _minimal_template()
    # ``flag`` is a declared column on the minimal template, so the
    # default resolver looks it up in the row dict.
    template_with_rule = _minimal_template(
        instinct_rules={
            "escalation": "lead",
            "rules": [{"when": "flag == 'go'", "action": "block"}],
        },
    )
    decision = resolve_instinct(template_with_rule, "go", {"flag": "go"}, now=FROZEN_NOW)
    assert decision.verdict == "BLOCK"
    # And the default resolver class is TemplateIdentifierResolver.
    assert isinstance(TemplateIdentifierResolver(template.state), TemplateIdentifierResolver)
