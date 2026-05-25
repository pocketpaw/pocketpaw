# ee/pocketpaw_ee/cloud/foresight/router.py
# Modified: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision) — PR 5
#   adds the per-anchor projection fanout surface:
#     GET /api/v1/foresight/runs/{id}/projected-decisions
#       → paginated list of projected decisions for one run, optional
#         ``anchor_id`` query filter. Tenancy: returns 404 when the run
#         is unknown / cross-tenant (same collapsing rule as
#         ``GET /runs/{id}``). Cursor: offset-based ``limit`` (default
#         50, capped at 500) + ``offset`` (default 0).
# Modified: 2026-05-25 (feat/foresight-v04-backtest-aggregator) — PR 4
#   adds the retroactive backtest gate surface:
#     POST /api/v1/foresight/backtests       → run a backtest + score it
#     GET  /api/v1/foresight/backtests/{id}  → fetch a stored backtest
#     GET  /api/v1/foresight/backtests       → list backtests in the workspace
#     GET  /api/v1/foresight/onboarding/gate → onboarding unlock state
#   All endpoints delegate to ``ee.cloud.foresight.service``; persistence
#   lives in the new ``foresight_backtests`` collection. The onboarding
#   UI flow that consumes the gate state belongs to a paw-enterprise PR.
# Modified: 2026-05-25 (feat/foresight-v07-cloud-mount) — PR 7. Routes now
#   delegate to ``ee.cloud.foresight.service`` instead of writing through
#   the in-memory ``RunStore``; ``GET /runs`` (list endpoint) added; the
#   router is mounted from ``mount_cloud`` (no more ``include_foresight_router``
#   helper). The v0.1 wire contract is preserved — POST /scenarios still
#   returns the completed run synchronously and GET /runs/{id} still
#   returns the same field set.
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Foresight REST surface — PR 4 contract:
#
#   POST /api/v1/foresight/scenarios       → run a scenario inline
#   GET  /api/v1/foresight/runs/{id}       → fetch a stored run
#   GET  /api/v1/foresight/runs            → list runs in the caller's workspace
#   POST /api/v1/foresight/backtests       → run a retroactive backtest + score
#   GET  /api/v1/foresight/backtests/{id}  → fetch a stored backtest
#   GET  /api/v1/foresight/backtests       → list backtests in the workspace
#   GET  /api/v1/foresight/onboarding/gate → onboarding unlock posture
#
# Mounted by ``ee.cloud.__init__:mount_cloud`` alongside the other cloud
# routers. Service-owned writes go to the ``ForesightRun`` +
# ``ForesightBacktest`` Beanie collections; persistence survives restarts
# and is workspace-scoped.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.dto import (
    BacktestRunListItemResponse,
    BacktestRunResponse,
    CreateBacktestRequest,
    CreateScenarioRequest,
    OnboardingGateResponse,
    ProjectedDecisionListResponse,
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


@router.get(
    "/runs/{run_id}/projected-decisions",
    response_model=ProjectedDecisionListResponse,
)
async def list_projected_decisions(
    run_id: str,
    anchor_id: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(request_context),
) -> ProjectedDecisionListResponse:
    """List projected decisions for one run.

    PR 5 contract:
      - Items are returned in ``(tick_id ASC, anchor_id ASC)`` order
        — the index on the persistence layer makes this a single
        bounded scan even across hundreds of records.
      - ``anchor_id`` query filter narrows to one anchor across all
        ticks (e.g. ``?anchor_id=segment:enterprise`` on a Market Sim
        run, ``?anchor_id=rollout:training`` on an Org Change run).
      - Tenancy: an unknown / cross-tenant run id returns 404
        (``foresight_run.not_found``) — same collapsing rule the
        scenario-run endpoints use so existence isn't cross-tenant
        leakable.
      - Pagination is offset-based; ``limit`` is hard-capped at 500.
        Cursor-based pagination lands in v1.0 once the dataset grows
        past the point where ``count_documents`` is cheap.
    """
    return await foresight_service.list_projected_decisions(
        ctx,
        run_id,
        anchor_id=anchor_id,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Backtest gate (RFC §10 + §13.1 gate 7)
# ---------------------------------------------------------------------------


@router.post("/backtests", response_model=BacktestRunResponse)
async def create_backtest(
    body: CreateBacktestRequest,
    ctx: RequestContext = Depends(request_context),
) -> BacktestRunResponse:
    """Run a retroactive backtest inline, score it against the unlock
    threshold, and return the result + gate decision.

    Body shape matches the forward-sim grammar (personas + sub_type +
    n_ticks) plus the ``anchors`` list — one historical decision per
    anchor with its known actual outcome inline. v0.1 takes the actuals
    inline; v1.0 will pull them from the Fabric/journal connector.

    Contract:
      - The backtest completes synchronously before this returns (same
        as v0.1's scenarios endpoint).
      - The response carries both ``result`` (engine wire dict +
        ``calibration_summary``) and ``gate_decision`` (the
        ThresholdDecision wire dict).
      - A passing backtest fires both ``foresight.backtest.completed``
        and ``foresight.onboarding.unlocked`` — the latter is the
        signal the chat agent's onboarding skill watches for.
      - Per-run thresholds may tighten above the workspace default
        (``GATE_DEFAULT_THRESHOLD = 0.65``) but cannot relax below it.
        A relaxation request returns 422 ``foresight.threshold_below_default``.
    """
    return await foresight_service.create_backtest(ctx, body)


@router.get("/backtests/{backtest_id}", response_model=BacktestRunResponse)
async def get_backtest(
    backtest_id: str,
    ctx: RequestContext = Depends(request_context),
) -> BacktestRunResponse:
    """Fetch a stored backtest by id.

    Returns 404 (``foresight_backtest.not_found``) for unknown,
    malformed, or cross-tenant ids — same collapsing rule the
    scenarios endpoint uses so existence isn't cross-tenant leakable.
    """
    return await foresight_service.get_backtest(ctx, backtest_id)


@router.get("/backtests", response_model=list[BacktestRunListItemResponse])
async def list_backtests(
    limit: int = Query(default=50, ge=1, le=200),
    ctx: RequestContext = Depends(request_context),
) -> list[BacktestRunListItemResponse]:
    """List backtests in the caller's workspace, most recent first.

    Lighter list-item shape keeps the per-row payload cheap; the
    detail endpoint serves the full result blob. ``gate_decision`` is
    preserved in the list shape so the Aggregate panel can render the
    pass / fail label per row without click-through.
    """
    return await foresight_service.list_backtests(ctx, limit=limit)


@router.get("/onboarding/gate", response_model=OnboardingGateResponse)
async def get_onboarding_gate(
    ctx: RequestContext = Depends(request_context),
) -> OnboardingGateResponse:
    """Return the workspace's onboarding unlock posture.

    Derived from the latest completed backtest in the workspace; the
    UI's onboarding flow polls this on the new-workspace path and the
    Scenarios panel checks ``unlocked`` before letting the operator
    start a forward sim.

    Reason vocabulary:
      - ``no_backtest`` — no backtest has run yet
      - ``in_flight`` — a backtest is queued / running, no prior pass
      - ``below_threshold`` — latest backtest failed the gate
      - ``unlocked`` — latest backtest passed; forward sims are open

    Note: the actual onboarding UI flow that consumes this state ships
    in a paw-enterprise PR (out of scope for the PocketPaw lane).
    """
    return await foresight_service.get_onboarding_gate(ctx)


__all__ = ["router"]
