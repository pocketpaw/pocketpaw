# domain.py — Frozen value objects for the pocket-outcomes entity.
# Created: 2026-05-22 (RFC 05 M2b.2) — `OutcomeRecord` is one row in the
#   workspace-scoped JSONL ledger. Tenancy (`workspace_id`) is a required
#   construction field per ee/cloud Rule 3 — a record cannot exist without
#   a workspace to scope it to.
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutcomeRecord:
    """One recorded pocket outcome — a checked business event.

    Built from a ``PocketOutcomeEvent`` and appended verbatim to the
    workspace JSONL ledger. ``outcome_value`` / ``outcome_unit`` are the
    Layer-4 billing slots — always ``None`` in this build, kept on the
    record so the ledger format is forward-stable when pricing lands.
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


__all__ = ["OutcomeRecord"]
