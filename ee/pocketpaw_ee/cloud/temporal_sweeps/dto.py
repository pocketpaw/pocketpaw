# ee/pocketpaw_ee/cloud/temporal_sweeps/dto.py
# Created: 2026-05-28 (feat/wave-3d-temporal-scheduler) — wire-format
# DTOs for the RFC 03 v2 temporal-sweep state surface. Distinct request
# and response classes per EE cloud rule 4 — never reuse one model for
# input and output.

"""Wire-format DTOs for the ``temporal_sweeps`` entity."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.temporal_sweeps.domain import (
    SweepDispatchResult,
    TemporalSweepState,
)


class ListSweepStateRequest(BaseModel):
    """Filter parameters for the per-pocket state-inspect endpoint.

    No filter today beyond the pocket id (URL param) — kept as a typed
    model so future fields (e.g. trigger_key filter, paging) do not
    break the wire shape.
    """

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=500, ge=1, le=5000)


class TemporalSweepStateResponse(BaseModel):
    """Wire response for one persisted (trigger, row) state row."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    pocket_id: str
    trigger_key: str
    row_id: str
    predicate_value: bool
    last_swept_at: str


class SweepDispatchResultResponse(BaseModel):
    """Wire response for one ``sweep_pocket`` invocation tally."""

    model_config = ConfigDict(extra="forbid")

    pocket_id: str
    edges_fired: int
    blocked: int
    escalated: int
    errors: int
    sweep_duration_ms: int


def state_to_dto(s: TemporalSweepState) -> TemporalSweepStateResponse:
    """Map a domain ``TemporalSweepState`` to its wire DTO."""
    return TemporalSweepStateResponse(
        workspace_id=s.workspace_id,
        pocket_id=s.pocket_id,
        trigger_key=s.trigger_key,
        row_id=s.row_id,
        predicate_value=s.predicate_value,
        last_swept_at=iso_utc(s.last_swept_at),
    )


def state_to_wire_dict(s: TemporalSweepState) -> dict:
    return state_to_dto(s).model_dump()


def dispatch_to_wire_dict(r: SweepDispatchResult) -> dict:
    return SweepDispatchResultResponse(
        pocket_id=r.pocket_id,
        edges_fired=r.edges_fired,
        blocked=r.blocked,
        escalated=r.escalated,
        errors=r.errors,
        sweep_duration_ms=r.sweep_duration_ms,
    ).model_dump()


__all__ = [
    "ListSweepStateRequest",
    "SweepDispatchResultResponse",
    "TemporalSweepStateResponse",
    "dispatch_to_wire_dict",
    "state_to_dto",
    "state_to_wire_dict",
]
