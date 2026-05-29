# ee/pocketpaw_ee/cloud/foresight/router.py
# Modified: 2026-05-29 (feat/foresight-rehearsals-joined) — v2 landing
# card hydration. Adds the joined ``/rehearsals`` listing endpoint:
#     GET /api/v1/foresight/rehearsals?limit=50&offset=0&sub_type=...
#       → RehearsalListResponse (paginated; each item carries
#         ``run_count`` + optional ``last_run`` summary so the v2
#         landing card can render without N+1 fetches).
#   Delegates to ``ee.cloud.foresight.scenarios.list_rehearsals`` per
#   cloud rule #2 (the joined read lives next to the workspace-scenarios
#   reads it builds on).
# Modified: 2026-05-26 (feat/foresight-v12-skill-and-loopback-auth) — RFC 08
# v1.0 wave 4. All foresight endpoints now resolve their RequestContext
# via ``loopback_or_request_context`` (the JWT-or-loopback dep). The
# local chat agent presents ``X-PocketPaw-Internal: true`` +
# ``X-PocketPaw-Workspace-Id`` + ``X-PocketPaw-User-Id`` headers over a
# loopback connection and skips JWT auth entirely; non-loopback or
# missing-header callers fall through to the standard
# ``current_optional_user`` flow (401 on no token). Endpoint signatures
# are unchanged — only the dep swap is touched.
# Modified: 2026-05-26 (feat/foresight-v10-insights-llm) — RFC 08 v1.0.
# LLM-driven insights synthesizer toggle:
#     GET /api/v1/foresight/workspace/insights-config
#       → ForesightInsightsConfigResponse (synthesizer +
#         llm_cache_ttl_seconds + updated_at). Workspace-scoped; an
#         absent config collapses to the default (synthesizer="pattern").
#     PUT /api/v1/foresight/workspace/insights-config
#       Body: { synthesizer: "pattern" | "llm" } — opts the workspace
#         into the LLM synthesizer (default stays "pattern"). 422 on
#         unknown synthesizer value (DTO-level enforcement); emits
#         ``foresight.insights_config.updated`` on effective change.
#   Both routes delegate to ``ee.cloud.foresight.service`` per cloud
#   rule #2. The /insights endpoint signature is UNCHANGED — only the
#   synthesizer implementation behind it swaps.
# Modified: 2026-05-26 (feat/foresight-v10-scenario-editor-backend) —
# RFC 08 v1.0 wave 3 adds the workspace-scoped custom-scenario CRUD:
#     GET    /api/v1/foresight/scenarios/custom
#       → CustomScenarioListResponse (paginated, optional sub_type filter)
#     GET    /api/v1/foresight/scenarios/custom/{id}
#       → CustomScenarioResponse (full yaml_body + parsed_meta)
#     POST   /api/v1/foresight/scenarios/custom
#       → CustomScenarioResponse (201)
#     PUT    /api/v1/foresight/scenarios/custom/{id}
#       → CustomScenarioResponse (full replace)
#     DELETE /api/v1/foresight/scenarios/custom/{id}
#       → 204 No Content
#   All five routes delegate to ``ee.cloud.foresight.scenarios`` (the
#   sibling service module to ``service.py`` — keeps the workspace-
#   scenario reads/writes scoped to a dedicated module so the
#   import-linter contract stays narrow).
#   Also: the existing POST /scenarios body now honors an optional
#   ``custom_scenario_id`` field (DTO change); when present the
#   server loads the workspace scenario's saved YAML and uses it for
#   the run.
# Modified: 2026-05-26 (feat/foresight-v10-threshold-override-cloud) —
# RFC 08 v1.0 PR 10 adds the per-workspace onboarding-gate threshold
# override surface:
#     GET /api/v1/foresight/workspace/threshold
#       → ForesightThresholdResponse (current / default / is_overridden /
#         updated_at). Workspace-scoped; no cross-tenant read possible
#         (the view is intrinsically per-workspace, and an absent
#         override collapses to the default).
#     PUT /api/v1/foresight/workspace/threshold
#       Body: { threshold: float | None } — float ∈ [0.5, 0.95] sets the
#         workspace override; null resets to the default.
#       → ForesightThresholdResponse (same shape, reflecting the new
#         state). 422 on out-of-bounds (DTO-level enforcement); 400 on
#         malformed body; emits ``foresight.threshold.updated`` on
#         effective-value changes.
#   Both routes delegate to ``ee.cloud.foresight.service`` per cloud
#   rule #2. paw-enterprise Team A2 builds the settings panel against
#   the locked field set above.
# Modified: 2026-05-26 (feat/foresight-v10-live-snapshot-and-fixes) —
# RFC 08 v1.0 PR adds the LivePanel backing endpoint:
#     GET /api/v1/foresight/runs/{id}/live-snapshot
#       → ``LiveSnapshotResponse`` (status + tier_mix_actual +
#         up-to-10 sampled traces + anomaly readouts). Cross-tenant
#         404 via the same ``_fetch_in_workspace`` rule the other
#         run-scoped reads use. Contract is locked to paw-enterprise
#         PR #267.
# Modified: 2026-05-25 (feat/foresight-v15-scenarios-aggregate-insights) —
# RFC 08 §11.2 / §11.5 / §11.6 backing endpoints:
#     GET /api/v1/foresight/scenarios   → ScenarioCatalogResponse
#         (bundled YAML template enumeration; workspace-agnostic).
#     GET /api/v1/foresight/aggregate   → AggregateRollupResponse
#         (rolling accuracy + drift + modal-outcome distribution over
#         a trailing window; default 30 days, max 90, 422 above).
#     GET /api/v1/foresight/insights    → InsightsResponse
#         (pattern-based synthesizer over the same aggregate inputs;
#         five v0.1 rules — accuracy_drop / persona_outlier /
#         tier_imbalance / trend_break / threshold_unmet; cap 20).
#   All three are read-only and delegate to ``ee.cloud.foresight.service``
#   per the cloud rule #2 (service-IS-the-repository). The §11 UI lead
#   builds its TypeScript surface against the exact shapes above.
# Modified: 2026-05-25 (feat/foresight-v08-approval-loop) — PR 8 / RFC 08 §8
#   adds the Foresight → Instinct approval-loop surface:
#     GET /api/v1/foresight/runs/{id}/instinct-proposals
#       → paginated list of Instinct rows spawned by this run. Tenancy:
#         404 when the run is unknown / cross-tenant (same collapsing
#         rule as the projection-list endpoint). Cursor: offset-based
#         ``limit`` (default 50, capped at 500) + ``offset`` (default 0).
#         Empty list when the run didn't opt into ``route_to_instinct``.
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

from fastapi import APIRouter, Depends, Query, Response, status

from pocketpaw_ee.cloud._core.context import RequestContext, loopback_or_request_context
from pocketpaw_ee.cloud.foresight import scenarios as foresight_scenarios
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.dto import (
    AggregateRollupResponse,
    BacktestRunListItemResponse,
    BacktestRunResponse,
    CreateBacktestRequest,
    CreateCustomScenarioRequest,
    CreateScenarioRequest,
    CustomScenarioListResponse,
    CustomScenarioResponse,
    ForesightInsightsConfigResponse,
    ForesightInstinctProposalListResponse,
    ForesightThresholdResponse,
    InsightsResponse,
    LiveSnapshotResponse,
    OnboardingGateResponse,
    ProjectedDecisionListResponse,
    RehearsalListResponse,
    ScenarioCatalogResponse,
    ScenarioRunListItemResponse,
    ScenarioRunResponse,
    SetForesightInsightsConfigRequest,
    SetForesightThresholdRequest,
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
    ctx: RequestContext = Depends(loopback_or_request_context),
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
    ctx: RequestContext = Depends(loopback_or_request_context),
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
    ctx: RequestContext = Depends(loopback_or_request_context),
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
    ctx: RequestContext = Depends(loopback_or_request_context),
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
# Live snapshot (RFC 08 §11.3 + paw-enterprise PR #267)
# ---------------------------------------------------------------------------


@router.get(
    "/runs/{run_id}/live-snapshot",
    response_model=LiveSnapshotResponse,
)
async def get_live_snapshot(
    run_id: str,
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> LiveSnapshotResponse:
    """Compact "right now" view of one Foresight run.

    Backs the paw-enterprise LivePanel. Contract is locked to
    paw-enterprise PR #267 — every field name and nesting shape on
    :class:`LiveSnapshotResponse` mirrors the TypeScript surface the
    UI was built against; this endpoint activates the LivePanel from
    its mock-fallthrough state.

    Tenancy: an unknown / cross-tenant run id returns 404
    (``foresight_run.not_found``) — same collapsing rule the other
    run-scoped endpoints use so existence isn't cross-tenant
    leakable.

    Status vocabulary on the wire is ``created | running | complete |
    failed``; the persisted ``queued`` state surfaces as ``created``.

    Empty / in-flight runs collapse to a zero ``tier_mix_actual``
    triple, empty ``sampled_traces``, and (usually) empty
    ``anomalies``. The UI's empty state renders directly off those
    defaults without a separate code path.

    Three v1.0 anomaly rules fire automatically:

      - ``tier_drift`` — actual tier mix off configured 5/15/80 by
        more than 0.15 (info) or 0.25 (warning).
      - ``confidence_spike`` — variance / mean extremes on the
        projection confidence distribution.
      - ``stalled_persona`` — persona behind the run's tick clock
        (warning) or silent while the run reached tick > 0 (critical).
    """
    return await foresight_service.get_live_snapshot(ctx, run_id)


# ---------------------------------------------------------------------------
# Foresight → Instinct approval loop (RFC 08 §8 + PR 8)
# ---------------------------------------------------------------------------


@router.get(
    "/runs/{run_id}/instinct-proposals",
    response_model=ForesightInstinctProposalListResponse,
)
async def list_instinct_proposals(
    run_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> ForesightInstinctProposalListResponse:
    """List the Instinct proposals spawned by one Foresight run.

    PR 8 contract (RFC 08 §8):
      - Returns the Instinct rows whose ``parameters._foresight.run_id``
        matches the run. Empty list when the run didn't opt into
        ``route_to_instinct`` or when the run hasn't ticked yet.
      - Each row is the EVIDENCE-only proposal the bridge spawned: the
        Instinct policy that already gates the underlying real decision
        still owns the predicate; approving a row acknowledges the
        forecast but does NOT trigger any executing side-effect.
      - Tenancy: an unknown / cross-tenant run id returns 404
        (``foresight_run.not_found``) — same collapsing rule the
        projection-list endpoint uses so existence isn't cross-tenant
        leakable. The Instinct store itself doesn't carry a
        ``workspace_id`` column (it's OSS-runtime SQLite), so the
        workspace check happens at the run level here.
      - Pagination is offset-based for parity with the projection-list
        endpoint; ``limit`` is hard-capped at 500.

    Operators who need the full Action payload (corrections, audit)
    fetch it via ``GET /api/v1/instinct/actions/{id}`` keyed by
    ``action_id``.
    """
    return await foresight_service.list_instinct_proposals_for_run(
        ctx,
        run_id,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Backtest gate (RFC §10 + §13.1 gate 7)
# ---------------------------------------------------------------------------


@router.post("/backtests", response_model=BacktestRunResponse)
async def create_backtest(
    body: CreateBacktestRequest,
    ctx: RequestContext = Depends(loopback_or_request_context),
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
    ctx: RequestContext = Depends(loopback_or_request_context),
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
    ctx: RequestContext = Depends(loopback_or_request_context),
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
    ctx: RequestContext = Depends(loopback_or_request_context),
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


# ---------------------------------------------------------------------------
# Scenarios catalog + Aggregate rollup + Insights (RFC §11.2 / §11.5 / §11.6)
# ---------------------------------------------------------------------------


@router.get("/scenarios", response_model=ScenarioCatalogResponse)
async def list_scenarios(
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> ScenarioCatalogResponse:
    """Enumerate the bundled scenario templates (RFC 08 §11.2).

    Static catalog of the YAML files under
    ``ee/pocketpaw_ee/foresight/scenarios/``. The response is small —
    one row per template — and changes only on code releases, so the
    service caches the catalog and invalidates on directory mtime
    change. Workspace-agnostic (the catalog is global), but the
    request_context dep stays so the route surfaces a clean 401 when
    auth fails upstream.

    v1.0 will surface workspace-owned scenarios (RFC §18 grammar)
    alongside the bundled ones — the response envelope is forward-
    compatible (additive ``items`` rows only).
    """
    return await foresight_service.list_scenarios(ctx)


@router.get("/aggregate", response_model=AggregateRollupResponse)
async def get_aggregate_rollup(
    window_days: int = Query(default=30, ge=1, le=90),
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> AggregateRollupResponse:
    """Rolling rollup over the workspace's recent backtests + projections
    (RFC 08 §11.5).

    ``window_days`` default is 30; values above 90 surface as 422
    ``foresight.invalid_window``. Empty workspaces collapse to zeros +
    empty arrays so the UI's Aggregate panel can render the empty
    state without a separate code path.

    v0.1 derives the rolling series from completed backtests'
    ``gate_decision.observed`` (PR 4's calibration buffer didn't ship
    Mongo persistence for the raw PredictionRecord shape); v1.0 will
    swap to the real per-pair series once the buffer migrates.

    The frontend Aggregate panel (RFC §11.5) reads
    ``rolling_accuracy.points`` for the trendline, ``confidence_drift``
    for the trend pill, and ``modal_outcome_distribution.entries`` for
    the per-outcome share chart.
    """
    return await foresight_service.get_aggregate_rollup(
        ctx,
        window_days=window_days,
    )


@router.get("/insights", response_model=InsightsResponse)
async def get_insights(
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> InsightsResponse:
    """Pattern-based insight synthesizer output (RFC 08 §11.6).

    v0.1 ships five rules driven by the same aggregate inputs the
    rollup endpoint reads — accuracy_drop / persona_outlier /
    tier_imbalance / trend_break / threshold_unmet. Items are sorted
    by severity descending (critical > warning > info) then
    generated_at descending; capped at 20 per response (pagination
    lands in v1.0 once the LLM synthesizer fans finer-grained rules).

    Empty workspaces return ``items=[]`` — the synthesizer yields no
    rows when none of the rules can fire. The frontend Insights panel
    (RFC §11.6) consumes the items directly and renders one card per
    row keyed on the stable ``id``.
    """
    return await foresight_service.get_insights(ctx)


# ---------------------------------------------------------------------------
# Per-workspace onboarding-gate threshold override (RFC 08 v1.0 PR 10)
# ---------------------------------------------------------------------------


@router.get(
    "/workspace/threshold",
    response_model=ForesightThresholdResponse,
)
async def get_workspace_threshold(
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> ForesightThresholdResponse:
    """Return the workspace's resolved onboarding-gate threshold view.

    Workspace-scoped — the view is intrinsically per-workspace and an
    absent override collapses to the default (``current_threshold ==
    default_threshold == 0.65``, ``is_overridden=False``,
    ``updated_at=null``). No cross-tenant read possible.

    Tenancy: 403 when the caller has no active workspace. Never 404.

    paw-enterprise Team A2's settings panel reads this on mount to
    render the override input pre-populated with the current value plus
    a "default 0.65" hint next to it.
    """
    return await foresight_service.get_threshold(ctx)


@router.put(
    "/workspace/threshold",
    response_model=ForesightThresholdResponse,
)
async def set_workspace_threshold(
    body: SetForesightThresholdRequest,
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> ForesightThresholdResponse:
    """Upsert the workspace's onboarding-gate threshold override.

    ``body.threshold = float`` in ``[0.5, 0.95]`` sets the workspace
    override (validated at the DTO layer — 422 fires before the service
    runs); ``body.threshold = null`` resets the workspace to the global
    default.

    Returns the same response shape as the GET so the UI can keep its
    local store in sync with one round trip.

    Side effects:
      - Upserts the
        :class:`pocketpaw_ee.cloud.models.foresight_workspace_config.ForesightWorkspaceConfig`
        doc keyed by ``workspace_id``.
      - Emits ``foresight.threshold.updated`` when the effective value
        changes; a no-op write (same value) stays quiet.

    Tenancy: 403 when no active workspace. The override applies to ALL
    future ``get_onboarding_gate`` reads and ALL future ``create_backtest``
    calls — a workspace that tightens its floor to 0.80 will reject
    per-run threshold requests below 0.80 starting on the next call.
    """
    return await foresight_service.set_threshold(ctx, body)


# ---------------------------------------------------------------------------
# Per-workspace insights-synthesizer toggle (RFC 08 v1.0 — LLM insights PR)
#
# Sibling endpoint to the threshold pair above. Workspaces opt into the
# LLM synthesizer here; the wire shape of /insights is unchanged either
# way (the toggle only swaps the synthesizer implementation behind the
# endpoint). Default stays ``"pattern"`` — cost discipline: the LLM
# path is opt-in, deterministic pattern rules stay free.
#
# Workspace-scoped custom scenarios (RFC 08 v1.0 wave 3) follow below.
# ---------------------------------------------------------------------------


@router.get(
    "/workspace/insights-config",
    response_model=ForesightInsightsConfigResponse,
)
async def get_workspace_insights_config(
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> ForesightInsightsConfigResponse:
    """Return the workspace's resolved insights-synthesizer config.

    Workspace-scoped — the view is intrinsically per-workspace and an
    absent config collapses to the default (``synthesizer="pattern"``,
    ``llm_cache_ttl_seconds=300``, ``updated_at=null``). No
    cross-tenant read possible.

    Tenancy: 403 when the caller has no active workspace. Never 404.

    paw-enterprise builds the Foresight admin panel against this read
    so the settings UI can show the current toggle plus the LLM cache
    TTL note without a hard-coded constant.
    """
    return await foresight_service.get_insights_config(ctx)


@router.put(
    "/workspace/insights-config",
    response_model=ForesightInsightsConfigResponse,
)
async def set_workspace_insights_config(
    body: SetForesightInsightsConfigRequest,
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> ForesightInsightsConfigResponse:
    """Upsert the workspace's insights-synthesizer choice.

    ``body.synthesizer = "pattern"`` keeps the v0.5 deterministic
    five-rule synthesizer (default). ``body.synthesizer = "llm"`` opts
    into the v1.0 LLM-driven synthesizer; LLM failures fall back to
    pattern at runtime so the wire response never 5xxs.

    Returns the same response shape as the GET so the UI can keep its
    local store in sync with one round trip.

    Side effects:
      - Upserts the
        :class:`pocketpaw_ee.cloud.models.foresight_workspace_config.ForesightWorkspaceConfig`
        doc keyed by ``workspace_id``.
      - Emits ``foresight.insights_config.updated`` when the effective
        synthesizer changes; a no-op write (same value) stays quiet.

    Tenancy: 403 when no active workspace. The toggle applies to ALL
    future ``GET /api/v1/foresight/insights`` reads.

    Cost discipline: opting into the LLM synthesizer triggers per-poll
    LLM round-trips (cached per workspace with a 5-minute TTL). The
    pattern synthesizer is deterministic and free; workspaces should
    only flip to "llm" when the operator wants richer pattern
    discovery and accepts the LLM cost.
    """
    return await foresight_service.set_insights_config(ctx, body)


@router.get(
    "/scenarios/custom",
    response_model=CustomScenarioListResponse,
)
async def list_custom_scenarios(
    sub_type: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> CustomScenarioListResponse:
    """List workspace-scoped custom scenarios, most-recently-edited first.

    The picker UI (Team 2) consumes this on the new-scenario sheet to
    let operators choose from saved YAMLs. Pagination is offset-based
    for parity with the projection-list / instinct-proposal endpoints;
    ``limit`` is hard-capped at 100.

    Optional ``sub_type`` filter narrows to ``decision_forecast`` /
    ``market_sim`` / ``org_change_rehearsal``. Tenancy: workspace-scoped
    via the dependency-resolved context; 403 when no active workspace.
    """
    return await foresight_scenarios.list_custom_scenarios(
        ctx,
        sub_type=sub_type,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/scenarios/custom/{scenario_id}",
    response_model=CustomScenarioResponse,
)
async def get_custom_scenario(
    scenario_id: str,
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> CustomScenarioResponse:
    """Fetch one custom scenario by id.

    Returns 404 (``foresight_custom_scenario.not_found``) for unknown,
    malformed, or cross-tenant ids — same collapsing rule the other
    foresight endpoints use so existence isn't leakable.
    """
    return await foresight_scenarios.get_custom_scenario(ctx, scenario_id)


@router.post(
    "/scenarios/custom",
    response_model=CustomScenarioResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_custom_scenario(
    body: CreateCustomScenarioRequest,
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> CustomScenarioResponse:
    """Save a new custom scenario YAML against the workspace.

    Validation flow (DTO → service):

      - DTO enforces field-shape bounds: ``name`` ≤120, ``description``
        ≤500, ``yaml_body`` ≤64 KB, ``sub_type`` in the supported set.
      - Service parses the YAML and validates engine grammar +
        v1.0 caps (persona/tick counts ≤100, tier_mix sums to 1.0 ±0.001,
        request ``sub_type`` matches YAML ``sub_type``).

    422 vocabulary:
      - ``foresight.invalid_yaml`` — YAML parse error.
      - ``foresight.sub_type_mismatch`` — request vs. YAML sub_type differ.
      - ``foresight.invalid_scenario`` — engine grammar / cap violation.

    Emits ``foresight.custom_scenario.created``.
    """
    return await foresight_scenarios.create_custom_scenario(ctx, body)


@router.put(
    "/scenarios/custom/{scenario_id}",
    response_model=CustomScenarioResponse,
)
async def update_custom_scenario(
    scenario_id: str,
    body: CreateCustomScenarioRequest,
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> CustomScenarioResponse:
    """Full-replace the custom scenario fields.

    Validation is identical to create. Author / workspace tenancy stay
    pinned to the original doc — the edit doesn't reassign the author
    column (the audit log is the source of truth for who edited).

    Emits ``foresight.custom_scenario.updated``.

    Returns 404 (``foresight_custom_scenario.not_found``) for unknown
    or cross-tenant ids; 422 on the same validation rules as create.
    """
    return await foresight_scenarios.update_custom_scenario(ctx, scenario_id, body)


@router.delete(
    "/scenarios/custom/{scenario_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_custom_scenario(
    scenario_id: str,
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> Response:
    """Remove the custom scenario row.

    Idempotency note: a second call against the same id returns 404 —
    we never silently no-op a delete against an unknown doc.

    Emits ``foresight.custom_scenario.deleted``.
    """
    await foresight_scenarios.delete_custom_scenario(ctx, scenario_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Rehearsals listing (v2 landing card hydration)
# ---------------------------------------------------------------------------


@router.get(
    "/rehearsals",
    response_model=RehearsalListResponse,
)
async def list_rehearsals(
    sub_type: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(loopback_or_request_context),
) -> RehearsalListResponse:
    """List the workspace's custom scenarios with joined run metadata.

    Backs the v2 ``/foresight`` landing's RehearsalCard hydration —
    every card needs ``run_count`` + a "last run was X" badge, and the
    landing renders ~10-50 cards on first paint. Doing this client-side
    would require an N+1 (list scenarios, then for each, list its runs);
    this endpoint folds the join into one round trip.

    Response shape mirrors :class:`CustomScenarioListResponse` (items /
    total / limit / offset / has_more) so the v2 landing's data hook
    can reuse the cursor logic. ``limit`` defaults to 50 (the v2
    landing's first-paint quota) and is hard-capped at 100.

    Optional ``sub_type`` filter narrows to ``decision_forecast`` /
    ``market_sim`` / ``org_change_rehearsal`` — same vocabulary the
    sibling ``/scenarios/custom`` endpoint accepts. Tenancy: 403 when
    no active workspace; runs from other workspaces never leak via the
    joined ``$in`` query (workspace filter is the leading clause).
    """
    return await foresight_scenarios.list_rehearsals(
        ctx,
        limit=limit,
        offset=offset,
        sub_type=sub_type,
    )


__all__ = ["router"]
