# dto.py — Request/response DTOs for the pocket-outcomes entity.
# Created: 2026-05-22 (RFC 05 M2b.2) — `CountOutcomesRequest` is the
#   validated query for `GET /api/v1/outcomes`; `OutcomeCountResponse` is
#   the grouped-count wire shape. Request and response are distinct models
#   per ee/cloud Rule 4.
# Updated: 2026-05-25 (RFC 07 Slice 2) — added `OutcomeResponse`, the
#   per-row wire shape mirroring `OutcomeRecord`. Carries the new
#   `decision_id` back-reference so the decision-graph trace can be
#   reached from the outcome and vice versa. No existing endpoint
#   returns a list of rows yet; the DTO is shipped now so future
#   `GET /api/v1/outcomes?detail=rows` lookups have a stable wire
#   contract and the back-reference is visible on the lint surface.
from __future__ import annotations

from pydantic import BaseModel, Field


class CountOutcomesRequest(BaseModel):
    """Validated query for ``GET /api/v1/outcomes``.

    ``workspace_id`` is taken from the auth context, never the query — the
    router rejects a ``workspace_id`` query param. ``pocket_id`` narrows
    to one pocket; ``since`` is an ISO-8601 lower bound (inclusive) on
    ``occurred_at``. Both optional.
    """

    pocket_id: str | None = None
    since: str | None = None


class OutcomeCountResponse(BaseModel):
    """Grouped outcome counts for a workspace.

    ``total`` is the count of every matching ledger row; ``by_outcome``
    maps each distinct ``outcome`` name to its count. ``by_pocket`` does
    the same per pocket id — handy for a workspace-wide rollup when no
    ``pocket_id`` filter was supplied.
    """

    total: int
    by_outcome: dict[str, int] = Field(default_factory=dict)
    by_pocket: dict[str, int] = Field(default_factory=dict)


class OutcomeResponse(BaseModel):
    """Wire shape for one recorded outcome (RFC 07 Slice 2).

    Mirrors `outcomes.domain.OutcomeRecord`. The `decision_id` field is
    the optional back-reference to the Decision in the RFC 07 decision
    graph that this outcome resolved — set when the producer
    (instinct bridge, pocket write executor) had a Decision in hand;
    `None` otherwise.
    """

    outcome: str
    pocket_id: str
    workspace_id: str
    action: str
    actor: str
    via_instinct: bool
    instinct_action_id: str | None = None
    occurred_at: str
    outcome_value: float | None = None
    outcome_unit: str | None = None
    decision_id: str | None = None


__all__ = ["CountOutcomesRequest", "OutcomeCountResponse", "OutcomeResponse"]
