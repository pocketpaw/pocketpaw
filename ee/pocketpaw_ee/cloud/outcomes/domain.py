# domain.py — Frozen value objects for the pocket-outcomes entity.
# Created: 2026-05-22 (RFC 05 M2b.2) — `OutcomeRecord` is one row in the
#   workspace-scoped JSONL ledger. Tenancy (`workspace_id`) is a required
#   construction field per ee/cloud Rule 3 — a record cannot exist without
#   a workspace to scope it to.
# Updated: 2026-05-25 (RFC 07 Slice 2) — added the `decision_id` back-
#   reference. The decision-graph projection folds journal events into
#   queryable Decisions; when an Outcome lands, the Decision needs to
#   know which Outcome resolved it. A producer (instinct bridge, pocket
#   write executor) that has a Decision in hand can pass `decision_id`
#   on `emit_pocket_outcome`; the listener then synthesises a
#   `decision.outcome_attached` journal event so the projection's
#   `_apply_outcome_attached` handler mutates the Decision in place.
#   Optional — pre-Slice-2 writers pass None and the back-reference is
#   simply absent from the ledger row.
# Updated: 2026-06-11 (gap-3 outcome VALUE metering) — `outcome_value` /
#   `outcome_unit` are no longer the always-`None` reserved slots. They
#   now carry the binding's author-time billable value/unit, persisted on
#   the ledger row so `meter_outcomes` can sum value by unit per workspace.
#   The fields and their defaults are unchanged; only the meaning is — a
#   pre-gap-3 ledger row (both `None`) reads back as a count-only outcome.
# Updated: 2026-06-01 (RFC 05 Saga Compensate) — added the optional
#   `compensated` flag. A compensating write (a rollback fired when a
#   multi-step write sequence failed partway) lands an outcome too — a
#   rollback is a real business event, not a silent side effect — but the
#   ledger must distinguish it from a forward outcome so a "renewal_sent"
#   that was later refunded does not over-count. Defaults False so every
#   pre-saga ledger row is byte-stable.
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutcomeRecord:
    """One recorded pocket outcome — a checked business event.

    Built from a ``PocketOutcomeEvent`` and appended verbatim to the
    workspace JSONL ledger. ``outcome_value`` / ``outcome_unit`` are the
    billable-value pair (gap-3): a real figure when the binding declared
    one, ``None``/``None`` for a count-only outcome. They are always a WHOLE
    pair or both ``None`` — ``record_outcome`` drops a torn half-pair to
    count-only so the aggregation can sum value strictly by unit.
    ``decision_id`` is the optional back-reference to the Decision in
    the RFC 07 decision graph that this outcome resolved — None for
    outcomes emitted by writers that don't know their Decision.
    """

    outcome: str
    pocket_id: str
    workspace_id: str
    action: str
    actor: str
    via_instinct: bool
    instinct_action_id: str | None
    occurred_at: str  # ISO-8601 UTC
    outcome_value: float | None = None
    outcome_unit: str | None = None
    decision_id: str | None = None  # RFC 07 Slice 2 back-reference
    # RFC 05 Saga Compensate — True when this outcome is a rollback (a
    # compensating write fired because a later step in a sequence failed),
    # False for an ordinary forward outcome. The meter can net these out.
    compensated: bool = False


__all__ = ["OutcomeRecord"]
