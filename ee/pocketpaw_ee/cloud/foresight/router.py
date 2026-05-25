# ee/pocketpaw_ee/cloud/foresight/router.py
# Modified: 2026-05-25 (feat/foresight-v07-cloud-mount) — PR 7. Routes now
#   delegate to ``ee.cloud.foresight.service`` instead of writing through
#   the in-memory ``RunStore``; ``GET /runs`` (list endpoint) added; the
#   router is mounted from ``mount_cloud`` (no more ``include_foresight_router``
#   helper). The v0.1 wire contract is preserved — POST /scenarios still
#   returns the completed run synchronously and GET /runs/{id} still
#   returns the same field set.
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Foresight REST surface — PR 7 contract:
#
#   POST /api/v1/foresight/scenarios     → run a scenario inline, return result
#   GET  /api/v1/foresight/runs/{id}     → fetch a stored run
#   GET  /api/v1/foresight/runs          → list runs in the caller's workspace
#
# Mounted by ``ee.cloud.__init__:mount_cloud`` alongside the other cloud
# routers. Service-owned writes go to the ``ForesightRun`` Beanie
# collection (see ``ee.cloud.models.foresight_run``); persistence
# survives restarts and is workspace-scoped.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.dto import (
    CreateScenarioRequest,
    ScenarioRunListItemResponse,
    ScenarioRunResponse,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/foresight",
    tags=["Foresight"],
    dependencies=[Depends(require_license)],
)


@router.post("/scenarios", response_model=ScenarioRunResponse)
async def create_scenario_run(
    body: CreateScenarioRequest,
    ctx: RequestContext = Depends(request_context),
) -> ScenarioRunResponse:
    """Run a scenario inline and return the result.

    PR 7 contract:
      - Body declares personas inline (no scenario library yet).
      - Backend is the deterministic fake (no API key required).
      - Run completes synchronously before the response returns.
      - Result is persisted in the ``foresight_runs`` Mongo collection
        so ``GET /runs/{id}`` returns the same payload across restarts.

    v1.0 will:
      - Accept ``scenario_id`` to reference a saved scenario.
      - Route to the configured backend tier-pool.
      - Return ``status="queued"`` with a websocket URL; the run
        fans out to a background task.
      - Emit ``foresight.run_started`` (in addition to the
        ``foresight.run.created`` PR 7 already emits) so the UI's
        Live panel can distinguish accepted-but-pending from actively
        ticking runs.
    """
    return await foresight_service.create_scenario_run(ctx, body)


@router.get("/runs/{run_id}", response_model=ScenarioRunResponse)
async def get_run(
    run_id: str,
    ctx: RequestContext = Depends(request_context),
) -> ScenarioRunResponse:
    """Fetch a stored run by id.

    Returns 404 (``foresight_run.not_found``) if the id is unknown or
    belongs to another workspace — existence is deliberately not
    leakable across tenants.
    """
    return await foresight_service.get_scenario_run(ctx, run_id)


@router.get("/runs", response_model=list[ScenarioRunListItemResponse])
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = Depends(request_context),
) -> list[ScenarioRunListItemResponse]:
    """List runs in the caller's workspace, most recent first.

    The frontend Scenarios panel (RFC §11.2) consumes this. The
    lighter list-item shape omits the inline ``result`` blob so a
    workspace with dozens of runs serves the list cheaply; click into
    a row to fetch the full :class:`ScenarioRunResponse` via the
    detail endpoint.
    """
    return await foresight_service.list_scenario_runs(ctx, limit=limit)


__all__ = ["router"]
