# dto.py — Request/response DTOs for the pocket-outcomes entity.
# Created: 2026-05-22 (RFC 05 M2b.2) — `CountOutcomesRequest` is the
#   validated query for `GET /api/v1/outcomes`; `OutcomeCountResponse` is
#   the grouped-count wire shape. Request and response are distinct models
#   per ee/cloud Rule 4.
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


__all__ = ["CountOutcomesRequest", "OutcomeCountResponse"]
