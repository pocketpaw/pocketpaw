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
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutcomeRecord:
    """One recorded pocket outcome — a checked business event.

    Built from a ``PocketOutcomeEvent`` and appended verbatim to the
    workspace JSONL ledger. ``outcome_value`` / ``outcome_unit`` are the
    Layer-4 billing slots — always ``None`` in this build, kept on the
    record so the ledger format is forward-stable when pricing lands.
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


__all__ = ["OutcomeRecord"]
