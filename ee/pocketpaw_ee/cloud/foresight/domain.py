# ee/pocketpaw_ee/cloud/foresight/domain.py
# Created: 2026-05-25 (feat/foresight-v07-cloud-mount) — RFC 08 PR 7.
#
# Foresight cloud domain — frozen value objects, no Beanie / Pydantic /
# FastAPI imports. The service (``ee.cloud.foresight.service``) maps
# between these and the ``ForesightRun`` Beanie document; the DTO layer
# (``ee.cloud.foresight.dto``) maps these to Pydantic responses.
#
# Multi-tenancy is enforced at construction per the cloud rule #3:
# ``workspace_id`` is required positionally with no default — building a
# ``ScenarioRun`` without one is a type error.
#
# Two value objects ship in PR 7:
#
#   - ``ScenarioRun`` — the persisted run record (mirrors RFC §7.7's
#     run-level fields: status, request, result, error).
#   - ``ProjectedDecision`` — the RFC §7.7 projected-decision shape
#     (run_id, sim_tick, projection_confidence). Not persisted in PR 7
#     (the engine emits projections inside ``RunResult.as_wire_dict``);
#     freezing the type now so PR 8's per-tick fan-out doesn't have to
#     reshape the surface.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

ScenarioRunStatus = Literal["queued", "running", "complete", "failed"]


@dataclass(frozen=True)
class ScenarioRun:
    """One Foresight scenario run, scoped to a workspace.

    Fields mirror the ``ForesightRun`` Beanie document plus the cloud
    rule #3 tenancy invariant. ``request`` is the validated POST body
    the operator submitted; ``result`` is the engine's
    ``RunResult.as_wire_dict()`` once the run completes; ``error`` is
    the failure message string when the run raises.

    ``id`` is the Mongo ``ObjectId`` rendered as a hex string — the wire
    contract the v0.1 in-memory store committed to (UUID strings via
    ``str(uuid4())``) is honoured by the response DTO, which normalizes
    both forms into a single string field.
    """

    id: str
    workspace_id: str
    scenario_name: str
    status: ScenarioRunStatus
    created_at: datetime
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    created_by: str = ""
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ProjectedDecision:
    """A Decision (RFC 07 shape) emitted inside a Foresight run.

    Same field set as the real Decision (anchor object, payload, actors)
    plus three Foresight-specific extras per RFC §7.7:

    - ``run_id``: the Foresight run that produced this projection.
    - ``sim_tick``: the simulation tick at which the chain closed.
    - ``projection_confidence``: aggregate confidence in (0.0, 1.0).

    Not persisted as its own Mongo collection in PR 7 — projected
    decisions live inside the run's ``result`` dict for now. PR 8 fans
    them out into a sibling ``projected_decisions`` collection so the
    Decision-Graph join (RFC §7.7 ``forward-precedent`` edge) can be
    indexed cheaply. Freezing the dataclass now so PR 8's persistence
    layer doesn't have to reshape the wire contract.
    """

    id: str
    workspace_id: str
    run_id: str
    sim_tick: int
    anchor_object_id: str
    payload: dict[str, Any]
    projection_confidence: float = 0.5
    actors: tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime | None = None


__all__ = [
    "ProjectedDecision",
    "ScenarioRun",
    "ScenarioRunStatus",
]
