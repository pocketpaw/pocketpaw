# ee/pocketpaw_ee/cloud/models/foresight_projected_decision.py
# Created: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision) —
# RFC 08 PR 5. ProjectedDecision Beanie document — RFC §7.7 per-anchor
# projection fanout, persisted as a sibling collection of
# ``foresight_runs`` + ``foresight_backtests``.
#
# Each (anchor × tick) bucket produces one document; the cloud's
# emit_projected_decision service function is the sole writer (per the
# cloud rule #2 — only ``ee.cloud.foresight.service`` may import this
# module). The import-linter contract in ``ee/pyproject.toml`` lists
# this doc alongside ``foresight_run`` + ``foresight_backtest``.
#
# Indexes match the two read paths PR 5 ships:
#   - List by run: ``(workspace, run_id, tick_id, anchor_id)`` so the
#     ``GET /runs/{id}/projected-decisions`` query is a single bounded
#     range scan ordered by tick then anchor.
#   - Filter by anchor across runs: ``(workspace, anchor_id, tick_id)``
#     so a query like "show me every projected decision against
#     lease:LR-2026-117" stays cheap even as the workspace accumulates
#     runs.
#
# The ``forward_precedent_decision_id`` field is RFC 07's Decision
# Graph hook — when a real Decision later lands that references this
# projection, the cloud's Decision Graph wiring (NOT in scope for PR 5)
# will populate this field with the real-decision id. v0.5 stubs it to
# ``None`` per the PR brief; RFC 07 lives only at /tmp/team-rfc07 per
# the soul memory and isn't yet integrated into pocketpaw.

from __future__ import annotations

from typing import Any

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class ForesightProjectedDecision(TimestampedDocument):
    """One projected decision emitted during a Foresight run.

    Fields:
      - ``workspace`` — tenancy key (Indexed for fast list queries).
      - ``run_id`` — the ForesightRun document id (hex string) the
        projection belongs to. Not a ``PydanticObjectId`` because the
        engine surface stays str-typed for cross-platform JSON.
      - ``anchor_id`` — sub-type-specific anchor identifier
        (``decision:<name>`` / ``segment:<role>`` / ``rollout:<event>``).
      - ``persona_id`` — the persona whose modal action drove the
        projection. Empty string when no persona acted for this anchor
        at this tick (the engine still emits a record so the per-anchor
        timeline stays dense).
      - ``tick_id`` — zero-based tick index inside the run.
      - ``decision_text`` — short string capturing the modal action
        verb (e.g. ``"accept"``, ``"churn"``, ``"escalate"``).
      - ``confidence`` — aggregate confidence in (0.0, 1.0). v0.5
        derives this from the share of personas whose action verb
        matched the modal bucket.
      - ``sub_type`` — the scenario's sub_type (echoed for downstream
        consumers that index per-anchor records across runs of
        different sub-types).
      - ``forward_precedent_decision_id`` — RFC 07 Decision Graph hook
        (RFC §7.7 forward-precedent edge). Stubbed to ``None`` in PR 5
        because RFC 07's Decision Graph wiring isn't in pocketpaw yet
        (scaffold lives at /tmp/team-rfc07 only). When the Decision
        Graph lands, a real Decision id will populate this field via
        a backfill pass scoped to the same workspace.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    run_id: str
    anchor_id: str
    persona_id: str = ""
    tick_id: int = Field(default=0, ge=0)
    decision_text: str = Field(default="noop", max_length=256)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    sub_type: str = Field(default="decision_forecast", max_length=64)
    forward_precedent_decision_id: str | None = None

    class Settings:
        name = "foresight_projected_decisions"
        indexes = [
            # Per-run listing — the GET /runs/{id}/projected-decisions
            # endpoint orders by (tick_id, anchor_id) inside one
            # workspace + run scope.
            [("workspace", 1), ("run_id", 1), ("tick_id", 1), ("anchor_id", 1)],
            # Anchor-across-runs lookup — the future Decision Graph
            # join (RFC §7.7 forward-precedent) walks projections by
            # anchor; the index keeps the query cheap when one anchor
            # accumulates dozens of projections across quarterly runs.
            [("workspace", 1), ("anchor_id", 1), ("tick_id", 1)],
        ]


__all__ = ["ForesightProjectedDecision"]


# Type alias for the dict shape the engine callback yields. Re-exported
# from this module so the service layer's emit function has a single
# canonical reference for the record shape.
ProjectedDecisionDict = dict[str, Any]
