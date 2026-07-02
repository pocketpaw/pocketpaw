# tests/unit/test_bulk_executor.py
# Updated: 2026-07-02 (AW-7 follow-up — security) — added the AW-7
#   TEMPLATE-level deny-on-no-match section: flag ON + mutating + no-rule-match
#   PARKS into the batch approval bucket; flag ON + read (is_mutating=False)
#   still FIRES; flag OFF (default) is unchanged; a matched execute/notify rule
#   is never re-bucketed by the flip.
# Created: 2026-05-28 (feat/rfc-03-v2-bulk) — unit tests for the bulk
# fan-out planner (``bulk_executor.plan_bulk_execution``). Pins the
# RFC 03 v2 bulk-action execution model: per-row Instinct composition,
# block / approval / execute / notify_and_execute bucketing, and the
# core invariant that ONE ``BulkApprovalRequest`` blesses the entire
# batch (not N requests). Worked-example coverage uses the bundled
# ``lease-renewal-v2.yaml`` fixture against the ``bulk_draft`` action;
# synthetic templates cover edge cases.
"""Tests for the bulk fan-out planner (RFC 03 v2 PR 2e).

The planner is a pure library function: given a template, a bulk
action name, and a list of selected rows, it returns a frozen
``BulkPlan`` describing what should happen per row PLUS the
single-batch approval contract from the RFC.

These tests pin:

* The core RFC contract — ONE ``BulkApprovalRequest`` covers ALL rows
  that need approval. Even if 50 rows escalate, one request goes on
  the queue, not 50.
* Per-row verdict bucketing — BLOCK rows go to ``blocked``,
  EXECUTE / NOTIFY_AND_EXECUTE rows go to ``executions`` (with
  ``notify_rules`` carried on the row), ESCALATE_APPROVAL rows
  consolidate into ``approval_request``.
* The ``mixed_approval_reasons`` consolidation — when one row
  escalates via overlay rules (``operator_overlay_escalated``) and
  another via author floor (``author_floor``), the consolidated
  ``BulkApprovalRequest.reason`` becomes ``"mixed_approval_reasons"``.
* Pre-flight validation — unknown action name and non-bulk action
  kind both raise ``BulkExecutionError``.
* Edge cases — empty selection, all-blocked batches, custom
  ``row_id_field``, deterministic ``now`` injection, resolver
  threading, and frozen-model immutability.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from pocketpaw.bundled_templates import (
    ActionDef,
    BlockedRow,
    BulkApprovalRequest,
    BulkExecutionError,
    IdentifierResolver,
    InstinctDecision,
    PocketTemplate,
    TemplateIdentifierResolver,
    plan_bulk_execution,
)

# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "templates" / "lease-renewal-v2.yaml"
)

# Fixed clock so the CEL ``within(...)`` helper is deterministic across
# the suite. Same wall-clock value tests/unit/test_instinct_composer.py
# uses so the two suites can share intuition.
FROZEN_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _load_lease_template() -> PocketTemplate:
    """Load the canonical worked-example fixture."""
    raw = yaml.safe_load(FIXTURE_PATH.read_text())
    return PocketTemplate.model_validate(raw)


def _clean_row(row_id: str, **overrides: Any) -> dict[str, Any]:
    """Return a lease row that matches NO instinct rule on the
    lease-renewal fixture. Tests override the bits they want to flip.

    The fixture's four rules reference ``rent_proposed``,
    ``rent_current``, ``rent_proposed_delta_pct``,
    ``tenant.late_payment_count_12mo``, ``expires_at``, and
    ``renewal_stage``. We populate every identifier so the resolver
    never trips ``KeyError`` mid-evaluation.
    """
    base: dict[str, Any] = {
        "id": row_id,
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


def _minimal_template(
    *,
    actions: list[dict[str, Any]] | None = None,
    rules: list[dict[str, str]] | None = None,
) -> PocketTemplate:
    """Build a tiny synthetic template that satisfies the schema. Used
    by edge-case tests that don't need the full lease fixture."""
    payload: dict[str, Any] = {
        "schema_version": "2",
        "name": "bulk-test",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "test",
        "description": "synthetic fixture for bulk executor tests",
        "shape": "data-grid",
        "state": {
            "entity_type": "Thing",
            "default_view": "list",
            "columns": [
                {"field": "id", "widget": "text"},
                {"field": "score", "widget": "number"},
            ],
        },
        "outcomes": ["thing_done"],
        "actions": actions
        or [
            {
                "name": "bulk_op",
                "label": "Bulk op",
                "kind": "bulk",
                "instinct_policy": "auto",
                "outcomes_emitted": ["thing_done"],
            }
        ],
    }
    if rules is not None:
        payload["instinct_rules"] = {"rules": rules}
    return PocketTemplate.model_validate(payload)


# ---------------------------------------------------------------------------
# Worked example — bulk_draft against lease-renewal-v2.yaml
# ---------------------------------------------------------------------------


def test_worked_example_all_clean_require_approval_consolidates_to_one_request() -> None:
    """All 3 rows clean against a ``require_approval`` policy → 1 batch
    approval covers all 3. This is the RFC's core promise: N rows, 1
    approval. The bulk_draft action carries
    ``instinct_policy: require_approval``."""
    template = _load_lease_template()
    rows = [
        _clean_row("lease-1"),
        _clean_row("lease-2"),
        _clean_row("lease-3"),
    ]
    plan = plan_bulk_execution(template, "bulk_draft", rows, now=FROZEN_NOW)

    assert plan.total_rows == 3
    assert plan.executions == []
    assert plan.blocked == []
    assert plan.approval_request is not None
    assert plan.approval_request.row_ids == ["lease-1", "lease-2", "lease-3"]
    # ``require_approval`` floor (no overlay rule matched) → author_floor.
    assert plan.approval_request.reason == "author_floor"
    # The approval request carries the full row data for audit.
    assert set(plan.approval_request.rows_data) == {"lease-1", "lease-2", "lease-3"}
    # No overlay rules fired, so matched_rules is empty.
    assert plan.approval_request.matched_rules == []


def test_worked_example_mixed_block_approval_clean() -> None:
    """Three rows, three buckets: lease-1 clean (→ approval via floor),
    lease-2 hits an overlay rule (→ approval via overlay), lease-3
    triggers the block rule (→ blocked). With bulk_draft's
    ``require_approval`` floor, NO row ever lands in ``executions``.
    The blocked row never enters the approval request — ``block always
    wins`` per RFC."""
    template = _load_lease_template()
    rows = [
        _clean_row("lease-1"),  # nothing matches → author_floor → approval
        _clean_row(
            "lease-2",
            # overlay: rent_proposed_delta_pct > 8 → require_approval
            rent_proposed_delta_pct=15.0,
        ),
        _clean_row(
            "lease-3",
            # block rule: rent_proposed < rent_current * 0.95
            rent_proposed=1700.0,
        ),
    ]
    plan = plan_bulk_execution(template, "bulk_draft", rows, now=FROZEN_NOW)

    assert plan.total_rows == 3
    assert plan.executions == []
    assert [b.row_id for b in plan.blocked] == ["lease-3"]
    assert plan.blocked[0].decision.verdict == "BLOCK"
    assert plan.blocked[0].blocked_by_rule.action == "block"

    assert plan.approval_request is not None
    # Blocked row excluded from approval ids.
    assert plan.approval_request.row_ids == ["lease-1", "lease-2"]
    # One row escalates via overlay, one via author_floor → mixed.
    assert plan.approval_request.reason == "mixed_approval_reasons"
    # The overlay-matched rule shows up in the union.
    assert any("rent_proposed_delta_pct" in r.when for r in plan.approval_request.matched_rules)


# ---------------------------------------------------------------------------
# Pure-bucket scenarios on a synthetic template
# ---------------------------------------------------------------------------


def test_all_clean_auto_policy_yields_executions_no_approval() -> None:
    """``auto`` policy + no rules → every row in ``executions`` with
    verdict EXECUTE. No approval request, no blocks."""
    template = _minimal_template()
    rows = [
        {"id": "r1", "score": 1},
        {"id": "r2", "score": 2},
        {"id": "r3", "score": 3},
    ]
    plan = plan_bulk_execution(template, "bulk_op", rows, now=FROZEN_NOW)

    assert plan.total_rows == 3
    assert [r.row_id for r in plan.executions] == ["r1", "r2", "r3"]
    assert all(r.verdict == "EXECUTE" for r in plan.executions)
    assert all(r.notify_rules == [] for r in plan.executions)
    assert plan.blocked == []
    assert plan.approval_request is None


def test_all_rows_blocked_yields_empty_executions_and_no_approval() -> None:
    """Every row trips a block rule → all rows in ``blocked``, zero
    executions, no approval request. Blocks must never sneak into the
    approval queue."""
    template = _minimal_template(
        rules=[{"when": "score < 100", "action": "block"}],
    )
    rows = [
        {"id": "r1", "score": 1},
        {"id": "r2", "score": 5},
    ]
    plan = plan_bulk_execution(template, "bulk_op", rows, now=FROZEN_NOW)

    assert plan.total_rows == 2
    assert plan.executions == []
    assert [b.row_id for b in plan.blocked] == ["r1", "r2"]
    assert plan.approval_request is None
    for blocked in plan.blocked:
        assert blocked.decision.verdict == "BLOCK"
        assert blocked.blocked_by_rule.action == "block"


def test_notify_only_policy_routes_rows_into_executions_with_notify_rules() -> None:
    """``notify_only`` policy + a matching notify rule → every row
    lands in ``executions`` with verdict NOTIFY_AND_EXECUTE and the
    matched notify rule carried on the row."""
    template = _minimal_template(
        actions=[
            {
                "name": "bulk_op",
                "label": "Bulk op",
                "kind": "bulk",
                "instinct_policy": "notify_only",
                "outcomes_emitted": ["thing_done"],
            }
        ],
        rules=[{"when": "score > 0", "action": "notify"}],
    )
    rows = [
        {"id": "r1", "score": 1},
        {"id": "r2", "score": 2},
    ]
    plan = plan_bulk_execution(template, "bulk_op", rows, now=FROZEN_NOW)

    assert plan.total_rows == 2
    assert [r.row_id for r in plan.executions] == ["r1", "r2"]
    assert all(r.verdict == "NOTIFY_AND_EXECUTE" for r in plan.executions)
    # Notify rule captured on each row's RowExecution.
    for row_exec in plan.executions:
        assert len(row_exec.notify_rules) == 1
        assert row_exec.notify_rules[0].action == "notify"
    assert plan.blocked == []
    assert plan.approval_request is None


def test_empty_selected_rows_returns_empty_plan() -> None:
    """Empty selection is valid — returns a fully-empty plan. Caller
    likely produced an empty filter result; do not crash."""
    template = _minimal_template()
    plan = plan_bulk_execution(template, "bulk_op", [], now=FROZEN_NOW)

    assert plan.total_rows == 0
    assert plan.executions == []
    assert plan.blocked == []
    assert plan.approval_request is None
    assert plan.action_name == "bulk_op"
    # The looked-up action is still carried for downstream inspection.
    assert plan.action.kind == "bulk"


# ---------------------------------------------------------------------------
# Approval-request consolidation semantics
# ---------------------------------------------------------------------------


def test_pure_author_floor_approval_reason_is_author_floor() -> None:
    """All rows clean, action floor is ``require_approval`` → reason
    ``author_floor`` (no overlay involvement)."""
    template = _minimal_template(
        actions=[
            {
                "name": "bulk_op",
                "label": "Bulk op",
                "kind": "bulk",
                "instinct_policy": "require_approval",
                "outcomes_emitted": ["thing_done"],
            }
        ],
    )
    rows = [{"id": "r1", "score": 1}, {"id": "r2", "score": 2}]
    plan = plan_bulk_execution(template, "bulk_op", rows, now=FROZEN_NOW)

    assert plan.approval_request is not None
    assert plan.approval_request.reason == "author_floor"
    assert plan.approval_request.matched_rules == []
    assert plan.approval_request.row_ids == ["r1", "r2"]


def test_pure_overlay_approval_reason_is_operator_overlay() -> None:
    """Every row matches an overlay approval rule under ``auto`` policy
    → reason ``operator_overlay_escalated`` (no author_floor in the
    mix)."""
    template = _minimal_template(
        rules=[{"when": "score > 0", "action": "require_approval"}],
    )
    rows = [{"id": "r1", "score": 1}, {"id": "r2", "score": 2}]
    plan = plan_bulk_execution(template, "bulk_op", rows, now=FROZEN_NOW)

    assert plan.approval_request is not None
    assert plan.approval_request.reason == "operator_overlay_escalated"
    assert plan.approval_request.row_ids == ["r1", "r2"]
    # Union carries one rule (same rule matched twice → still one in
    # the union).
    assert len(plan.approval_request.matched_rules) == 1
    assert plan.approval_request.matched_rules[0].action == "require_approval"


def test_mixed_approval_reasons_when_some_overlay_some_floor() -> None:
    """Action floor is ``require_approval``. Row r1 hits an overlay
    rule (operator-escalated); row r2 matches nothing (author_floor).
    The consolidated request flips to ``mixed_approval_reasons``."""
    template = _minimal_template(
        actions=[
            {
                "name": "bulk_op",
                "label": "Bulk op",
                "kind": "bulk",
                "instinct_policy": "require_approval",
                "outcomes_emitted": ["thing_done"],
            }
        ],
        rules=[{"when": "score > 10", "action": "require_approval"}],
    )
    rows = [
        {"id": "r1", "score": 20},  # overlay matches → operator_overlay
        {"id": "r2", "score": 1},  # no overlay → author_floor
    ]
    plan = plan_bulk_execution(template, "bulk_op", rows, now=FROZEN_NOW)

    assert plan.approval_request is not None
    assert plan.approval_request.reason == "mixed_approval_reasons"
    assert plan.approval_request.row_ids == ["r1", "r2"]
    # Audit trail preserves per-row decisions for the approval queue.
    assert set(plan.approval_request.per_row_decisions) == {"r1", "r2"}
    assert plan.approval_request.per_row_decisions["r1"].reason == "operator_overlay_escalated"
    assert plan.approval_request.per_row_decisions["r2"].reason == "author_floor"


# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------


def test_unknown_action_raises_bulk_execution_error() -> None:
    """Action name not declared on the template → BulkExecutionError
    with ``unknown action`` in the message. The error must surface
    BEFORE any row evaluation runs."""
    template = _minimal_template()
    with pytest.raises(BulkExecutionError, match="unknown action"):
        plan_bulk_execution(template, "does_not_exist", [{"id": "r1"}], now=FROZEN_NOW)


def test_non_bulk_action_raises_bulk_execution_error() -> None:
    """Action exists but ``kind`` is ``single-row`` → BulkExecutionError.
    The planner is for ``kind: bulk`` only; routing the wrong action
    here would silently mis-execute."""
    template = _minimal_template(
        actions=[
            {
                "name": "row_op",
                "label": "Row op",
                "kind": "single-row",
                "instinct_policy": "auto",
                "outcomes_emitted": ["thing_done"],
            },
            {
                "name": "global_op",
                "label": "Global op",
                "kind": "global",
                "instinct_policy": "auto",
                "outcomes_emitted": ["thing_done"],
            },
        ],
    )
    with pytest.raises(BulkExecutionError, match="not a bulk action"):
        plan_bulk_execution(template, "row_op", [{"id": "r1"}], now=FROZEN_NOW)
    with pytest.raises(BulkExecutionError, match="not a bulk action"):
        plan_bulk_execution(template, "global_op", [{"id": "r1"}], now=FROZEN_NOW)


# ---------------------------------------------------------------------------
# Row-ID resolution
# ---------------------------------------------------------------------------


def test_row_id_field_defaults_to_template_state_id_field() -> None:
    """When no override is passed, the planner uses
    ``template.state.id_field`` (which itself defaults to ``"id"``)."""
    template = _minimal_template()
    rows = [{"id": "r1", "score": 1}, {"id": "r2", "score": 2}]
    plan = plan_bulk_execution(template, "bulk_op", rows, now=FROZEN_NOW)
    assert [r.row_id for r in plan.executions] == ["r1", "r2"]


def test_row_id_field_override_uses_caller_supplied_field() -> None:
    """Passing ``row_id_field`` overrides the template's default. The
    approval request's row_ids derive from the override."""
    template = _minimal_template(
        actions=[
            {
                "name": "bulk_op",
                "label": "Bulk op",
                "kind": "bulk",
                "instinct_policy": "require_approval",
                "outcomes_emitted": ["thing_done"],
            }
        ],
    )
    rows = [
        {"id": "ignored-1", "score": 1, "external_id": "ext-A"},
        {"id": "ignored-2", "score": 2, "external_id": "ext-B"},
    ]
    plan = plan_bulk_execution(
        template, "bulk_op", rows, row_id_field="external_id", now=FROZEN_NOW
    )
    assert plan.approval_request is not None
    assert plan.approval_request.row_ids == ["ext-A", "ext-B"]


def test_row_id_field_uses_template_state_id_field_when_declared() -> None:
    """Template declares ``state.id_field: lease_id`` → planner picks
    it up automatically, no override needed."""
    payload = {
        "schema_version": "2",
        "name": "id-field-test",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "test",
        "description": "synthetic",
        "shape": "data-grid",
        "state": {
            "entity_type": "Lease",
            "id_field": "lease_id",
            "default_view": "list",
            "columns": [
                {"field": "lease_id", "widget": "text"},
                {"field": "score", "widget": "number"},
            ],
        },
        "outcomes": ["done"],
        "actions": [
            {
                "name": "bulk_op",
                "label": "Bulk op",
                "kind": "bulk",
                "instinct_policy": "auto",
                "outcomes_emitted": ["done"],
            }
        ],
    }
    template = PocketTemplate.model_validate(payload)
    rows = [
        {"lease_id": "L-1", "score": 1, "id": "ignored"},
        {"lease_id": "L-2", "score": 2, "id": "ignored"},
    ]
    plan = plan_bulk_execution(template, "bulk_op", rows, now=FROZEN_NOW)
    assert [r.row_id for r in plan.executions] == ["L-1", "L-2"]


# ---------------------------------------------------------------------------
# Determinism, resolver injection, immutability
# ---------------------------------------------------------------------------


def test_plan_is_deterministic_for_same_now_and_rows() -> None:
    """Two calls with the same template, same rows, same ``now`` →
    identical plans. Pure-function invariant."""
    template = _load_lease_template()
    rows = [_clean_row("lease-1"), _clean_row("lease-2", rent_proposed=1700.0)]
    plan_a = plan_bulk_execution(template, "bulk_draft", rows, now=FROZEN_NOW)
    plan_b = plan_bulk_execution(template, "bulk_draft", rows, now=FROZEN_NOW)
    assert plan_a == plan_b


def test_custom_resolver_is_threaded_into_resolve_instinct() -> None:
    """Passing a custom resolver routes through to ``resolve_instinct``
    for every row. We assert observable behaviour: a resolver that
    raises ``KeyError`` for a needed identifier surfaces as a
    BulkExecutionError-or-CEL-failure (composer raises
    InstinctResolutionError, which we expose by letting it bubble)."""

    class _CountingResolver:
        """Records every ``resolve()`` call to confirm threading."""

        def __init__(self, inner: IdentifierResolver) -> None:
            self.inner = inner
            self.calls: list[str] = []

        def resolve(self, path: str, context: dict[str, Any]) -> Any:
            self.calls.append(path)
            return self.inner.resolve(path, context)

    template = _minimal_template(
        rules=[{"when": "score > 0", "action": "require_approval"}],
    )
    counting = _CountingResolver(TemplateIdentifierResolver(template))
    rows = [{"id": "r1", "score": 1}, {"id": "r2", "score": 2}]
    plan = plan_bulk_execution(template, "bulk_op", rows, resolver=counting, now=FROZEN_NOW)

    # The rule references ``score``; the resolver fired at least
    # once per row.
    assert plan.total_rows == 2
    assert counting.calls.count("score") >= 2


def test_bulk_plan_is_frozen() -> None:
    """``BulkPlan`` must be immutable so callers can pass it through
    audit / serialization layers without worrying about mutation."""
    template = _minimal_template()
    plan = plan_bulk_execution(template, "bulk_op", [{"id": "r1", "score": 1}], now=FROZEN_NOW)
    with pytest.raises(ValidationError):
        plan.total_rows = 99  # type: ignore[misc]


def test_row_execution_is_frozen() -> None:
    """``RowExecution`` must be immutable too."""
    template = _minimal_template()
    plan = plan_bulk_execution(template, "bulk_op", [{"id": "r1", "score": 1}], now=FROZEN_NOW)
    row_exec = plan.executions[0]
    with pytest.raises(ValidationError):
        row_exec.row_id = "mutated"  # type: ignore[misc]


def test_bulk_approval_request_is_frozen() -> None:
    """``BulkApprovalRequest`` must be immutable so the EE runtime
    can't accidentally rewrite an in-flight approval."""
    template = _minimal_template(
        actions=[
            {
                "name": "bulk_op",
                "label": "Bulk op",
                "kind": "bulk",
                "instinct_policy": "require_approval",
                "outcomes_emitted": ["thing_done"],
            }
        ],
    )
    plan = plan_bulk_execution(template, "bulk_op", [{"id": "r1", "score": 1}], now=FROZEN_NOW)
    assert plan.approval_request is not None
    with pytest.raises(ValidationError):
        plan.approval_request.row_ids = []  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Type-shape sanity
# ---------------------------------------------------------------------------


def test_bulk_plan_carries_action_def_for_downstream_dispatch() -> None:
    """The plan exposes the resolved ``ActionDef`` so the EE runtime
    doesn't have to look it up again. This is the only non-frozen-list
    field we sanity-check here; the action object itself comes from
    Pydantic and is already validated upstream."""
    template = _minimal_template()
    plan = plan_bulk_execution(template, "bulk_op", [], now=FROZEN_NOW)
    assert isinstance(plan.action, ActionDef)
    assert plan.action.name == "bulk_op"
    assert plan.action.kind == "bulk"


def test_executions_carry_full_instinct_decision() -> None:
    """Each ``RowExecution`` carries the underlying ``InstinctDecision``
    so callers can audit *why* a row landed in ``executions``."""
    template = _minimal_template()
    plan = plan_bulk_execution(template, "bulk_op", [{"id": "r1", "score": 1}], now=FROZEN_NOW)
    row_exec = plan.executions[0]
    assert isinstance(row_exec.decision, InstinctDecision)
    assert row_exec.decision.verdict == "EXECUTE"
    assert row_exec.decision.reason == "auto"


def test_blocked_row_carries_full_instinct_decision_and_rule() -> None:
    """Each ``BlockedRow`` carries the full decision *and* the first
    block rule that fired — both are needed for the audit log."""
    template = _minimal_template(
        rules=[{"when": "score < 100", "action": "block"}],
    )
    plan = plan_bulk_execution(template, "bulk_op", [{"id": "r1", "score": 1}], now=FROZEN_NOW)
    blocked = plan.blocked[0]
    assert isinstance(blocked, BlockedRow)
    assert isinstance(blocked.decision, InstinctDecision)
    assert blocked.decision.verdict == "BLOCK"
    assert blocked.blocked_by_rule.action == "block"
    assert blocked.row == {"id": "r1", "score": 1}


def test_bulk_approval_request_type_check() -> None:
    """Sanity: the approval request is a ``BulkApprovalRequest``
    instance, not a dict or a plain Decision."""
    template = _minimal_template(
        actions=[
            {
                "name": "bulk_op",
                "label": "Bulk op",
                "kind": "bulk",
                "instinct_policy": "require_approval",
                "outcomes_emitted": ["thing_done"],
            }
        ],
    )
    plan = plan_bulk_execution(template, "bulk_op", [{"id": "r1", "score": 1}], now=FROZEN_NOW)
    assert isinstance(plan.approval_request, BulkApprovalRequest)


# ---------------------------------------------------------------------------
# AW-7 — TEMPLATE-level deny-on-no-match (bulk security hole fix)
# ---------------------------------------------------------------------------
#
# Added: 2026-07-02 (AW-7 follow-up — security). AW-7 threaded the
# per-workspace ``instinct_template_default_deny`` flag through the router +
# temporal dispatcher, but the bulk fan-out still bucketed a no-rule-match
# EXECUTE (reason "auto") into ``executions`` — which ``_fire_executions`` fires
# un-re-gated — so an uncovered MUTATING bulk action executed ungated with the
# flag ON. ``plan_bulk_execution`` now takes ``template_default_deny`` +
# ``is_mutating`` (EE-computed) and, when both are True, routes a no-rule-match
# default EXECUTE into the batch APPROVAL bucket instead. These tests pin:
#   * flag ON + mutating + no-rule-match  → PARKS (approval bucket, not fired)
#   * flag ON + read (is_mutating=False)  → FIRES (a read has nothing to govern)
#   * flag OFF (default)                  → UNCHANGED (row fires)
#   * scope: a MATCHED execute/notify rule is NEVER re-bucketed by the flip.


def test_aw7_flag_on_mutating_no_rule_match_parks_into_approval() -> None:
    """Flag ON + is_mutating + a clean row (no rule matches → verdict
    EXECUTE, reason 'auto') → the row is routed to the batch APPROVAL
    bucket, NOT ``executions``. This is the bulk deny-by-default fix."""
    template = _minimal_template()  # auto policy, no rules
    rows = [
        {"id": "r1", "score": 1},
        {"id": "r2", "score": 2},
    ]
    plan = plan_bulk_execution(
        template,
        "bulk_op",
        rows,
        now=FROZEN_NOW,
        template_default_deny=True,
        is_mutating=True,
    )

    # No row fires — every uncovered mutating row parked.
    assert plan.executions == []
    assert plan.blocked == []
    # ONE consolidated batch approval covers both rows.
    assert plan.approval_request is not None
    assert plan.approval_request.row_ids == ["r1", "r2"]
    # A pure deny-on-no-match batch gets its own truthful reason.
    assert plan.approval_request.reason == "template_default_deny"
    # The per-row decisions still carry the underlying EXECUTE/auto verdict.
    for rid in ("r1", "r2"):
        decision = plan.approval_request.per_row_decisions[rid]
        assert decision.verdict == "EXECUTE"
        assert decision.reason == "auto"


def test_aw7_flag_on_read_only_still_fires() -> None:
    """Flag ON but is_mutating=False (a read: read_only marker / GET /
    HEAD) → the row still FIRES. A read has nothing to govern, so the
    deny-by-default flip never gates it."""
    template = _minimal_template()  # auto policy, no rules
    rows = [{"id": "r1", "score": 1}]
    plan = plan_bulk_execution(
        template,
        "bulk_op",
        rows,
        now=FROZEN_NOW,
        template_default_deny=True,
        is_mutating=False,
    )

    assert [r.row_id for r in plan.executions] == ["r1"]
    assert all(r.verdict == "EXECUTE" for r in plan.executions)
    assert plan.approval_request is None


def test_aw7_flag_off_is_unchanged() -> None:
    """Flag OFF (the default) → identical to before AW-7: an uncovered
    mutating row fires. This is the day-one, zero-behavior-change path."""
    template = _minimal_template()  # auto policy, no rules
    rows = [{"id": "r1", "score": 1}, {"id": "r2", "score": 2}]

    # Explicit OFF.
    plan_off = plan_bulk_execution(
        template,
        "bulk_op",
        rows,
        now=FROZEN_NOW,
        template_default_deny=False,
        is_mutating=True,
    )
    assert [r.row_id for r in plan_off.executions] == ["r1", "r2"]
    assert plan_off.approval_request is None

    # Default (no kwargs) must match explicit OFF exactly.
    plan_default = plan_bulk_execution(template, "bulk_op", rows, now=FROZEN_NOW)
    assert [r.row_id for r in plan_default.executions] == ["r1", "r2"]
    assert plan_default.approval_request is None


def test_aw7_notify_only_author_floor_is_never_re_bucketed() -> None:
    """Scope guard: the flip is the no-rule-match ('auto') default ONLY. An
    action with a ``notify_only`` author floor returns NOTIFY_AND_EXECUTE
    (reason 'notify_only', NOT 'auto') — an explicit policy decision — so it
    keeps firing even with the flag ON + is_mutating; it is never overridden."""
    template = _minimal_template(
        actions=[
            {
                "name": "bulk_op",
                "label": "Bulk op",
                "kind": "bulk",
                "instinct_policy": "notify_only",
                "outcomes_emitted": ["thing_done"],
            }
        ],
    )
    rows = [{"id": "r1", "score": 1}]
    plan = plan_bulk_execution(
        template,
        "bulk_op",
        rows,
        now=FROZEN_NOW,
        template_default_deny=True,
        is_mutating=True,
    )

    # The notify_only author floor → NOTIFY_AND_EXECUTE fires, not parked.
    assert [r.row_id for r in plan.executions] == ["r1"]
    assert plan.executions[0].verdict == "NOTIFY_AND_EXECUTE"
    assert plan.executions[0].decision.reason == "notify_only"
    assert plan.approval_request is None
