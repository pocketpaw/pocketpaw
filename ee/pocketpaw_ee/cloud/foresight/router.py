# ee/pocketpaw_ee/cloud/foresight/router.py
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Foresight REST surface — v0.1 contract:
#
#   POST /api/v1/foresight/scenarios   → run a scenario inline, return result
#   GET  /api/v1/foresight/runs/{id}   → fetch a stored run
#
# v0.1 runs synchronously (the smoke loop is fast — milliseconds with
# DeterministicFakeBackend) and persists results in the in-memory
# RunStore from ee/pocketpaw_ee/foresight/api/run_store.py. v1.0 swaps
# the store for Beanie documents, adds a service module, fans the run
# out to a background task, and exposes a websocket for live tick
# updates (RFC §11.3 Live panel).
#
# Not yet wired into mount_cloud — wiring lands in the follow-up PR
# that adds the cloud rule #1 4-file shape (domain.py + service.py).
# v0.1 ships the router behind an explicit ``include_foresight_router``
# helper so the contract is testable without committing to the mount
# order.

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.foresight.dto import (
    CreateScenarioRequest,
    ScenarioRunResponse,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.foresight.api.run_store import (
    RunStore,
    get_run_store,
    record_to_wire,
)
from pocketpaw_ee.foresight.persona import OceanDrift
from pocketpaw_ee.foresight.scenarios.runner import (
    PersonaSpec,
    ScenarioConfig,
    run_scenario,
)

router = APIRouter(
    prefix="/foresight",
    tags=["Foresight"],
    dependencies=[Depends(require_license)],
)


@router.post("/scenarios", response_model=ScenarioRunResponse)
async def create_scenario_run(
    body: CreateScenarioRequest,
    ctx: RequestContext = Depends(request_context),
    store: RunStore = Depends(get_run_store),
) -> ScenarioRunResponse:
    """Run a scenario inline and return the result.

    v0.1 contract:
      - Body declares personas inline (no scenario library yet).
      - Backend is the deterministic fake (no API key required).
      - Run completes synchronously before the response returns.
      - Result is also persisted in the in-memory RunStore so
        ``GET /runs/{id}`` returns the same payload.

    v1.0 will:
      - Accept ``scenario_id`` to reference a saved scenario.
      - Route to the configured backend tier-pool.
      - Return ``status="queued"`` with a websocket URL; the run
        fans out to a background task.
      - Emit ``foresight.run_started`` and ``foresight.run_complete``
        events on the InProcessBus (RFC §17 cross-references).
    """
    # Cloud rule #6: re-parse at service entry. The router has already
    # parsed via FastAPI but the equivalent service function (v1.0)
    # will be called from bus handlers / MCP tools / CLI — keep the
    # pattern visible even when the router is the only caller.
    body = CreateScenarioRequest.model_validate(body)

    try:
        personas = [
            PersonaSpec(
                name=p.name,
                role=p.role,
                ocean=OceanDrift(**p.ocean),
            )
            for p in body.personas
        ]
        config = ScenarioConfig(
            name=body.name,
            sub_type=body.sub_type,
            n_ticks=body.n_ticks,
            personas=personas,
        )
    except (TypeError, ValueError, NotImplementedError) as exc:
        # ConfigValidation maps to 422 — keep the error code namespaced
        # to the foresight surface so dashboards can filter on it.
        raise ValidationError("foresight.invalid_scenario", str(exc)) from exc

    record = store.create(
        scenario_name=body.name,
        request=body.model_dump(),
    )

    store.mark_running(record.id)
    try:
        result = await run_scenario(config)
    except Exception as exc:  # noqa: BLE001 — capture into the store, never bubble
        store.mark_failed(record.id, f"{type(exc).__name__}: {exc}")
        # Re-fetch so the response carries the failure marker.
        record = store.get(record.id)  # type: ignore[assignment]
        assert record is not None  # noqa: S101 — invariant; we just created it
        return ScenarioRunResponse.model_validate(record_to_wire(record))

    store.mark_complete(record.id, result.as_wire_dict())
    record = store.get(record.id)  # type: ignore[assignment]
    assert record is not None  # noqa: S101 — invariant; we just created it

    # v1.0 will emit `foresight.run_complete` here via the InProcessBus
    # so the UI rail's Live panel can refresh and the calibration loop
    # can pick up the prediction buffer entries. v0.1 stops at the
    # store write — no event emission yet, no calibration capture.
    return ScenarioRunResponse.model_validate(record_to_wire(record))


@router.get("/runs/{run_id}", response_model=ScenarioRunResponse)
async def get_run(
    run_id: str,
    ctx: RequestContext = Depends(request_context),
    store: RunStore = Depends(get_run_store),
) -> ScenarioRunResponse:
    """Fetch a stored run by id.

    Returns 404 (``foresight_run.not_found``) if the id is unknown,
    422 (``foresight.invalid_run_id``) if the id is not a valid UUID.
    v1.0 adds RBAC filtering (you can only see runs for workspaces
    you have access to) once the cloud service module lands.
    """
    try:
        rid = UUID(run_id)
    except ValueError as exc:
        raise ValidationError(
            "foresight.invalid_run_id",
            f"run id must be a UUID, got {run_id!r}",
        ) from exc
    record = store.get(rid)
    if record is None:
        raise NotFound("foresight_run", run_id)
    return ScenarioRunResponse.model_validate(record_to_wire(record))
