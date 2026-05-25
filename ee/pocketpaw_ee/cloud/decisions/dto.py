# dto.py — Request / response DTOs for the decision-graph REST surface.
# Created: 2026-05-25 (RFC 07 Slice 1) — skeletons only. Slice 1 ships the
#   in-process Python API (`DecisionGraph` in service.py) which returns
#   `Decision` domain objects directly. The REST router fills in shape
#   in Slice 2 — these DTOs are the wire contract it will use.
#
# Why ship the DTOs now: Slice 2 work can start without re-deriving the
# wire shape, and the import-linter contract (decisions/dto.py forbidden
# from importing models.*) needs a real file to lint. Distinct
# Request/Response per ee/cloud Rule 4.
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from pocketpaw_ee.cloud.decisions.domain import (
    Decision,
    DecisionEdgeRecord,
    OutcomeStatus,
    ScopeKind,
)

# ---------------------------------------------------------------------------
# GET /api/v1/decisions  (Slice 2)
# ---------------------------------------------------------------------------


class DecisionsListRequest(BaseModel):
    """Validated query for `GET /api/v1/decisions` (Slice 2).

    ``workspace_id`` is taken from auth context, never the query.
    Pagination is keyset-style (RFC perf budget — never OFFSET at scale):
    ``before_ts`` + ``before_id`` carry the cursor from the previous page.
    """

    model_config = ConfigDict(frozen=True)

    actor: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    scope_kind: ScopeKind | None = None
    pocket_id: str | None = None
    policy: str | None = None
    outcome_status: OutcomeStatus | None = None
    input_id: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    # keyset pagination cursor (sort key: ts DESC, id DESC). Slice 2 wires.
    before_ts: datetime | None = None
    before_id: str | None = None


class DecisionsListResponse(BaseModel):
    """List response. ``total`` is post-scope-filter — never the
    pre-filter count (RFC 07 § Privacy + audit; matches FabricProjection
    invariant).
    """

    model_config = ConfigDict(frozen=True)

    decisions: list[Decision] = Field(default_factory=list)
    total: int = 0
    next_before_ts: datetime | None = None
    next_before_id: str | None = None


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/:id/trace  (Slice 2)
# ---------------------------------------------------------------------------


class DecisionTraceRequest(BaseModel):
    """Validated query for `GET /api/v1/decisions/:id/trace?depth=N`."""

    model_config = ConfigDict(frozen=True)

    depth: int = Field(default=3, ge=1, le=10)
    max_fanout: int = Field(default=20, ge=1, le=100)


class TraceNodeWire(BaseModel):
    """One node in the trace response wire shape (Slice 2)."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: str  # decision | fabric_object | dataref
    decision: Decision | None = None
    label: str = ""


class DecisionTraceResponse(BaseModel):
    """BFS trace response (Slice 2). ``truncated`` is set when any node
    exceeded the fanout cap; ``truncated_count`` reports how many edges
    were dropped (RFC 07 amendment for gap G7).
    """

    model_config = ConfigDict(frozen=True)

    root: str
    nodes: dict[str, TraceNodeWire] = Field(default_factory=dict)
    edges: list[DecisionEdgeRecord] = Field(default_factory=list)
    truncated: bool = False
    truncated_count: int = 0
    depth_reached: int = 0


__all__ = [
    "DecisionTraceRequest",
    "DecisionTraceResponse",
    "DecisionsListRequest",
    "DecisionsListResponse",
    "TraceNodeWire",
]
