# ee/pocketpaw_ee/cloud/foresight/service.py
# Updated: 2026-05-25 (feat/foresight-v15-scenarios-aggregate-insights) —
# RFC 08 §11.2 / §11.5 / §11.6 backing service functions:
#   - ``list_scenarios(ctx)`` — enumerates the bundled YAML templates
#     at ``ee/pocketpaw_ee/foresight/scenarios/*.yaml``. Loader is
#     cached at module import (a touch on disk reloads via the
#     ``_SCENARIOS_CATALOG`` sentinel) and never touches the engine
#     persona / LLM / substrate dependencies — only ``yaml.safe_load``.
#   - ``get_aggregate_rollup(ctx, window_days)`` — derives the rolling
#     accuracy series + confidence drift + modal-outcome distribution
#     from the workspace's persisted ``ForesightBacktest`` +
#     ``ForesightProjectedDecision`` docs. Empty workspaces collapse
#     to zeros + empty arrays (never 404). Window capped at 90;
#     above 90 → 422 ``foresight.invalid_window``.
#   - ``get_insights(ctx)`` — composes the SynthesizerInput from the
#     same aggregate inputs (rolling series, drift, modal dist, latest
#     backtest gate, per-persona calibration proxy, tier-distribution
#     deltas) and calls ``ee.foresight.insights.synthesize_insights``
#     for the five v0.1 pattern rules. Cap 20.
#   v0.1 NOTE: PR 4's CalibrationPair persistence didn't land — the
#   aggregate + insights paths read backtest summaries + projected
#   decisions as proxies. Documented in the PR body; v1.0 swaps to a
#   real PredictionRecord collection once the calibration buffer
#   migrates to Mongo.
# Updated: 2026-05-25 (feat/foresight-v08-approval-loop) — PR 8 §14.4 wire:
#   - ``emit_projected_decision`` now accepts an optional
#     ``forward_precedent_decision_id`` kwarg and persists it on the
#     ``ForesightProjectedDecision`` doc instead of hardcoding ``None``.
#     The cloud-side closure in ``_run_engine_inline`` resolves the id
#     via ``ee.foresight.decision_graph_ref.NoOpDecisionGraphRef`` (PR
#     #1235 §14.4) so the persisted doc matches the engine's
#     ``RunResult.projected_decisions`` shape one-to-one. The cloud
#     body does not yet expose ``precedent_seed`` so the ref is seeded
#     empty by default — every lookup returns ``None`` and behaviour
#     is unchanged until either a body extension lands or RFC 07's
#     real Decision Graph implementation replaces the NoOp ref.
# Updated: 2026-05-25 (feat/foresight-v08-approval-loop) — PR 8 / RFC 08 §8:
#   - ``CreateScenarioRequest.route_to_instinct`` threads through
#     ``_run_engine_inline`` into the per-tick callback. When the flag
#     is true, ``emit_projected_decision`` also fans the projection
#     into the Instinct approval queue via
#     ``ee.foresight.instinct_bridge.projected_decision_to_instinct_proposal``
#     + the global ``InstinctStore`` (lazy import — the engine layer
#     stays clean of cloud, and the cloud module never grew a static
#     ``pocketpaw.instinct`` dep). The fan-out is idempotent: before
#     proposing, the service scans existing Instinct rows scoped to
#     the run's synthetic ``pocket_id`` and skips when an Action with
#     the same dedupe key already exists. Backtests never opt in —
#     ``create_backtest`` builds its scenario body with
#     ``route_to_instinct=False`` explicitly.
#   - Added ``list_instinct_proposals_for_run(ctx, run_id, limit, offset)``
#     — the GET endpoint reader. Returns the Instinct rows whose
#     ``parameters._foresight.run_id`` matches the run, scoped to
#     the caller's workspace via the same ``_fetch_in_workspace``
#     404-collapse rule the projection-list endpoint uses.
# Updated: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision) — PR 5:
#   - Added the RFC §7.7 per-anchor projection fanout. The engine call
#     (``_run_engine_inline``) now accepts a ``run_id`` + an injected
#     per-tick callback. The callback (``emit_projected_decision``) is
#     the engine → cloud direction: defined here in cloud, passed by
#     closure into the engine's ``run_scenario`` so the import-linter's
#     "engine never imports cloud" contract holds. Every (anchor × tick)
#     bucket gets one ForesightProjectedDecision document plus a
#     ``ForesightProjectedDecisionEmitted`` event so the Live panel can
#     render the timeline without polling.
#   - Added ``list_projected_decisions(ctx, run_id, anchor_id=None,
#     limit=50, offset=0)`` — the GET endpoint reader. Tenancy + run
#     scoping enforced via the ``_fetch_in_workspace`` helper that
#     already collapses unknown runs / cross-tenant ids into 404; the
#     ``anchor_id`` filter is the additional optional clause. v0.5
#     keeps the cursor offset-based.
# Updated: 2026-05-25 (feat/foresight-v04-backtest-aggregator) — PR 4:
#   - Added retroactive backtest API (``create_backtest`` /
#     ``get_backtest`` / ``list_backtests``) and the onboarding gate
#     reader (``get_onboarding_gate``). Backtests live in their own
#     ``foresight_backtests`` collection (sibling of ``foresight_runs``);
#     only this service module imports the document, per import-linter.
#   - The engine helper is shared between scenarios and backtests via
#     ``_run_engine_inline``; backtests additionally pair the engine's
#     projected outcomes against the operator-supplied actual outcomes
#     (RFC §10) and run the aggregator (``aggregate_pairs`` +
#     ``accuracy_meets_threshold``) to compute the gate decision.
#   - Default gate threshold is ``GATE_DEFAULT_THRESHOLD = 0.65`` (captain
#     locked for v0.1 — v1.0 reads a workspace-config override). The DTO
#     accepts a per-run override but only above the default — relaxing
#     the bar below the workspace default is rejected as a 422 so an
#     overeager operator can't trivially open the gate.
# Created: 2026-05-25 (feat/foresight-v07-cloud-mount) — RFC 08 PR 7.
#
# Foresight cloud service — business logic for scenario runs + backtests.
# Sole owner of writes to the ``ForesightRun`` + ``ForesightBacktest``
# Beanie documents per the cloud rule #2; module-level ``async def``
# functions per rule #5; ``RequestContext`` first; validate-at-entry;
# emit on every state-mutating write.
#
# Public API:
#   - ``create_scenario_run(ctx, body)`` — POST /foresight/scenarios
#   - ``get_scenario_run(ctx, run_id)`` — GET /foresight/runs/{id}
#   - ``list_scenario_runs(ctx)`` — GET /foresight/runs
#   - ``create_backtest(ctx, body)`` — POST /foresight/backtests
#   - ``get_backtest(ctx, backtest_id)`` — GET /foresight/backtests/{id}
#   - ``list_backtests(ctx)`` — GET /foresight/backtests
#   - ``get_onboarding_gate(ctx)`` — GET /foresight/onboarding/gate
#
# The engine call itself (``run_scenario`` from the foresight runtime)
# stays synchronous inside ``create_scenario_run`` — PR 7 keeps the v0.1
# request/response contract (run completes before POST returns) so
# existing tests + the smoke loop are undisturbed. v1.0 fans the run
# out to a background task and the POST returns immediately with
# ``status="queued"``.
#
# Engine import is lazy: the cloud surface stays clean of any
# ``ee.foresight.{persona,llm,scenarios}`` imports until the moment the
# service actually runs a scenario, so importing the cloud module never
# drags in CAMEL or the OASIS substrate (those land via PR 2's
# ``pocketpaw-ee[foresight]`` optional extra). The aggregator
# (``ee.foresight.aggregator``) is similarly lazy — imported inside
# ``_score_backtest`` rather than at module top.

from __future__ import annotations

import logging
from datetime import UTC as UTC_TZ
from datetime import datetime, timedelta
from typing import Any, Literal

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    ForesightBacktestCompleted,
    ForesightBacktestCreated,
    ForesightBacktestFailed,
    ForesightInstinctProposalCreated,
    ForesightOnboardingUnlocked,
    ForesightProjectedDecisionEmitted,
    ForesightRunCompleted,
    ForesightRunCreated,
    ForesightRunFailed,
)
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.foresight.domain import (
    AggregateRollup,
    BacktestRun,
    ConfidenceDrift,
    InsightView,
    ModalOutcomeEntry,
    OnboardingGateState,
    ProjectedDecision,
    RollingAccuracyPoint,
    ScenarioCatalogEntry,
    ScenarioRun,
)
from pocketpaw_ee.cloud.foresight.dto import (
    AggregateRollupResponse,
    BacktestRunListItemResponse,
    BacktestRunResponse,
    ConfidenceDriftDto,
    CreateBacktestRequest,
    CreateScenarioRequest,
    ForesightInstinctProposalListResponse,
    ForesightInstinctProposalResponse,
    InsightResponse,
    InsightsResponse,
    ModalOutcomeDistributionDto,
    ModalOutcomeEntryDto,
    OnboardingGateResponse,
    ProjectedDecisionListResponse,
    ProjectedDecisionResponse,
    RollingAccuracyPointDto,
    RollingAccuracySeriesDto,
    ScenarioCatalogItem,
    ScenarioCatalogResponse,
    ScenarioRunListItemResponse,
    ScenarioRunResponse,
)
from pocketpaw_ee.cloud.models.foresight_backtest import (
    ForesightBacktest as _ForesightBacktestDoc,
)
from pocketpaw_ee.cloud.models.foresight_projected_decision import (
    ForesightProjectedDecision as _ForesightProjectedDecisionDoc,
)
from pocketpaw_ee.cloud.models.foresight_run import ForesightRun as _ForesightRunDoc

# Default onboarding gate threshold (RFC §13.1 gate 7 — captain locked for
# v0.1 per PR 4 brief; v1.0 ops UI will let workspace admins tune).
GATE_DEFAULT_THRESHOLD: float = 0.65

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping helpers (kept private; cloud rule #8 prefers Pydantic mapping but
# the ``request`` / ``result`` ``dict[str, Any]`` fields don't benefit from
# ``from_attributes`` since they're already JSON-shaped).
# ---------------------------------------------------------------------------


def _to_domain(doc: _ForesightRunDoc) -> ScenarioRun:
    return ScenarioRun(
        id=str(doc.id),
        workspace_id=doc.workspace,
        scenario_name=doc.scenario_name,
        status=doc.status,  # type: ignore[arg-type]
        created_at=doc.createdAt,
        request=dict(doc.request or {}),
        result=dict(doc.result) if doc.result else None,
        error=doc.error,
        created_by=doc.created_by,
        updated_at=getattr(doc, "updatedAt", None),
    )


def _to_response(run: ScenarioRun) -> ScenarioRunResponse:
    return ScenarioRunResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        scenario_name=run.scenario_name,
        status=run.status,
        created_at=iso_utc(run.created_at) or "",
        updated_at=iso_utc(run.updated_at),
        request=dict(run.request),
        result=dict(run.result) if run.result else None,
        error=run.error,
    )


def _to_list_item_response(run: ScenarioRun) -> ScenarioRunListItemResponse:
    return ScenarioRunListItemResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        scenario_name=run.scenario_name,
        status=run.status,
        created_at=iso_utc(run.created_at) or "",
        updated_at=iso_utc(run.updated_at),
        error=run.error,
    )


# ---------------------------------------------------------------------------
# Tenancy helpers
# ---------------------------------------------------------------------------


def _require_workspace(ctx: RequestContext) -> str:
    """Foresight always operates in a workspace; routes that bypass an
    active workspace should never reach the service. Raise a Forbidden
    so the caller gets a clean 403 rather than a 500."""
    if not ctx.workspace_id:
        raise Forbidden(
            "foresight.no_workspace",
            "Active workspace required for foresight operations",
        )
    return ctx.workspace_id


async def _fetch_in_workspace(workspace_id: str, run_id: str) -> _ForesightRunDoc:
    """Fetch a run scoped to the caller's workspace; raise NotFound if
    the id is malformed, the doc is missing, or it lives in another
    workspace (so we don't leak existence across tenants)."""
    try:
        oid = PydanticObjectId(run_id)
    except Exception:
        raise NotFound("foresight_run", run_id) from None
    doc = await _ForesightRunDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("foresight_run", run_id)
    return doc


# ---------------------------------------------------------------------------
# Engine call — kept behind a lazy import so the cloud module never pulls
# in CAMEL / OASIS / Claude SDK on import.
# ---------------------------------------------------------------------------


async def _run_engine_inline(
    body: CreateScenarioRequest,
    *,
    workspace_id: str | None = None,
    run_id: str | None = None,
    route_to_instinct: bool = False,
) -> dict[str, Any]:
    """Drive the foresight engine for one scenario run.

    Imports are lazy: the engine modules (persona, llm, scenarios) live
    under ``ee.pocketpaw_ee.foresight.*`` and the cloud layer must not
    statically import them per the cloud-vs-engine separation. The
    cloud rule #2 forbids touching the Beanie doc outside this service;
    the analogous principle on the engine side is that the cloud
    surface must remain importable without the engine's optional deps.

    PR 5 wires the per-tick ProjectedDecision callback. When the caller
    supplies a ``workspace_id`` + ``run_id``, the runner emits one
    ForesightProjectedDecision document per (anchor × tick) bucket via
    the ``emit_projected_decision`` closure below. The closure stays
    here (rather than in the runner) so the engine never statically
    imports the cloud — the import-linter contract holds and the
    engine's optional-extra story is preserved.

    Returns the engine's ``RunResult.as_wire_dict()`` so the caller can
    persist the run as a JSON-shaped blob without leaking dataclass
    types into the persistence layer.
    """
    from pocketpaw_ee.foresight.decision_graph_ref import NoOpDecisionGraphRef
    from pocketpaw_ee.foresight.persona import OceanDrift
    from pocketpaw_ee.foresight.scenarios.runner import (
        PersonaSpec,
        ScenarioConfig,
        run_scenario,
    )

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

    # v14 (§14.4) — build a DecisionGraphRef mirroring the engine's
    # default so the cloud closure can stamp the same
    # ``forward_precedent_decision_id`` value the runner computes for
    # ``RunResult.projected_decisions``. The cloud's
    # ``CreateScenarioRequest`` body does not yet expose
    # ``precedent_seed`` / ``precedent_seeds`` (engine YAML can carry
    # them; cloud body extension lands later), so this ref is seeded
    # empty by default — :class:`NoOpDecisionGraphRef` returns ``None``
    # for every lookup in that case, preserving the v0.1 behaviour the
    # callers expect. Real RFC 07 wiring drops in by swapping the
    # implementation here once the Decision Graph lands in pocketpaw.
    decision_graph_ref = NoOpDecisionGraphRef(seed="")

    # Per-tick emission closure — only wired when the cloud caller
    # supplies the run id. CLI smoke runs pass no run_id and the
    # callback stays None; the engine still surfaces the records on
    # ``RunResult.projected_decisions`` either way.
    #
    # PR 8 (RFC 08 §8): the closure forwards ``route_to_instinct`` and
    # the originating scenario name into ``emit_projected_decision`` so
    # the cloud-side fan-out can spawn an Instinct proposal per
    # projection when the scenario opted in. The engine layer itself
    # stays unaware of Instinct — it only sees the projection callback
    # signature it already supports.
    #
    # v14 (§14.4): the closure also resolves the forward-precedent id
    # via the cloud-side DecisionGraphRef so the persisted
    # ``ForesightProjectedDecision`` doc carries the same value the
    # engine writes into ``RunResult.projected_decisions``. The lookup
    # is pure / deterministic — same inputs always produce the same id
    # — so the engine + cloud paths stay in sync without coordination.
    callback = None
    if workspace_id and run_id:
        scenario_name = body.name

        async def _on_projected_decision(
            anchor_id: str,
            persona_id: str,
            tick_id: int,
            decision_text: str,
            confidence: float,
            sub_type: str,
        ) -> None:
            precedent_id = decision_graph_ref.lookup_precedent(
                anchor_id=anchor_id,
                persona_id=persona_id,
                scenario_id=scenario_name,
            )
            await emit_projected_decision(
                workspace_id=workspace_id,
                run_id=run_id,
                anchor_id=anchor_id,
                persona_id=persona_id,
                tick_id=tick_id,
                decision_text=decision_text,
                confidence=confidence,
                sub_type=sub_type,
                forward_precedent_decision_id=precedent_id,
                route_to_instinct=route_to_instinct,
                scenario_name=scenario_name,
            )

        callback = _on_projected_decision

    result = await run_scenario(
        config,
        on_projected_decision=callback,
        run_id=run_id,
    )
    return result.as_wire_dict()


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------


async def create_scenario_run(
    ctx: RequestContext, body: CreateScenarioRequest
) -> ScenarioRunResponse:
    """Insert a run document, drive the engine inline, persist the result.

    Three writes happen here:

      1. Insert the doc with ``status="queued"`` so the run has an id
         immediately (even though PR 7 keeps the run synchronous,
         persisting the queued state makes the v1.0 background-task
         migration mechanical — POST will simply return after step 1).
      2. Save with ``status="running"`` before the engine call.
      3. Save with ``status="complete"`` + ``result`` after success, or
         ``status="failed"`` + ``error`` on engine failure.

    Each write emits its own event so listeners (the UI rail's Live
    panel, the v1.0 calibration loop) can react incrementally instead
    of polling.

    Engine failures are caught and persisted as ``status="failed"`` —
    we never let an engine exception bubble out, because that would
    leave the run document orphaned in ``status="running"``.
    """
    body = CreateScenarioRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)

    # Lazy import inside the engine helper; constructing the config also
    # validates engine-side rules (sub_type, n_ticks, personas) so we
    # surface those as 422 before opening a doc row.
    try:
        # Build the config once to surface engine-side validation errors
        # before persisting; the actual run is driven from
        # ``_run_engine_inline`` below to keep the import lazy.
        from pocketpaw_ee.foresight.persona import OceanDrift
        from pocketpaw_ee.foresight.scenarios.runner import PersonaSpec, ScenarioConfig

        _ = ScenarioConfig(
            name=body.name,
            sub_type=body.sub_type,
            n_ticks=body.n_ticks,
            personas=[
                PersonaSpec(
                    name=p.name,
                    role=p.role,
                    ocean=OceanDrift(**p.ocean),
                )
                for p in body.personas
            ],
        )
    except (TypeError, ValueError, NotImplementedError) as exc:
        raise ValidationError("foresight.invalid_scenario", str(exc)) from exc

    doc = _ForesightRunDoc(
        workspace=workspace_id,
        scenario_name=body.name,
        status="queued",
        request=body.model_dump(),
        created_by=ctx.user_id,
    )
    await doc.insert()

    created_response = _to_response(_to_domain(doc))
    await emit(ForesightRunCreated(data=created_response.model_dump()))

    # Mark running before the engine call so observers see the
    # transition. The save also bumps ``updatedAt`` via the
    # TimestampedDocument hook.
    doc.status = "running"
    await doc.save()
    # no-event: the ``running`` transition is an implementation detail of
    # PR 7's inline run mode — the v1.0 background-task migration will
    # emit a dedicated ``ForesightRunStarted`` event from the worker.
    # PR 7 keeps the v0.1 event vocabulary (created → completed/failed).

    try:
        result_dict = await _run_engine_inline(
            body,
            workspace_id=workspace_id,
            run_id=str(doc.id),
            # PR 8 (RFC 08 §8) — forward the operator's opt-in flag into the
            # per-tick fanout closure. When false (the default), the
            # projection-only fan-out runs and no Instinct rows are created.
            # When true, ``emit_projected_decision`` also fans an evidence
            # proposal into the Instinct queue.
            route_to_instinct=body.route_to_instinct,
        )
    except Exception as exc:  # noqa: BLE001 — capture into the doc, never bubble
        error_message = f"{type(exc).__name__}: {exc}"
        doc.status = "failed"
        doc.error = error_message
        await doc.save()
        failed_response = _to_response(_to_domain(doc))
        await emit(ForesightRunFailed(data=failed_response.model_dump()))
        logger.warning(
            "foresight.create_scenario_run: engine failed for run %s in ws=%s: %s",
            doc.id,
            workspace_id,
            error_message,
        )
        return failed_response

    doc.status = "complete"
    doc.result = result_dict
    await doc.save()

    completed_response = _to_response(_to_domain(doc))
    await emit(ForesightRunCompleted(data=completed_response.model_dump()))
    return completed_response


async def get_scenario_run(ctx: RequestContext, run_id: str) -> ScenarioRunResponse:
    """Fetch a single run by id, scoped to the caller's workspace.

    Returns 404 (``foresight_run.not_found``) if the id is unknown,
    malformed, or belongs to another tenant — we deliberately collapse
    "wrong workspace" into "not found" so existence isn't leakable
    across tenants.
    """
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_in_workspace(workspace_id, run_id)
    # no-event: read-only path; emit only on writes (cloud rule #9).
    return _to_response(_to_domain(doc))


async def list_scenario_runs(
    ctx: RequestContext, *, limit: int = 50
) -> list[ScenarioRunListItemResponse]:
    """List runs in the caller's workspace, most recent first.

    ``limit`` caps the response at 50 by default; the frontend's
    Scenarios panel paginates beyond that. The lighter
    :class:`ScenarioRunListItemResponse` shape drops the inline
    ``result`` blob so a workspace with a hundred runs still serves
    the list endpoint in tens of kilobytes rather than megabytes.
    """
    workspace_id = _require_workspace(ctx)
    if limit < 1:
        raise ValidationError("foresight.invalid_limit", "limit must be >= 1")
    if limit > 200:
        # Hard cap so a misconfigured caller can't drag the entire
        # collection into memory; the frontend never asks for more.
        limit = 200

    # Tenant filter on every read per cloud rule #7. Sort newest first
    # so the Scenarios panel renders most-recent-on-top without a
    # client-side reorder pass. ``_id`` tiebreaker keeps the ordering
    # stable when ``createdAt`` collides at sub-millisecond resolution
    # (a hot create loop will produce ties under in-memory Mongo and
    # under sub-millisecond Mongo clocks in production).
    docs = (
        await _ForesightRunDoc.find({"workspace": workspace_id})
        .sort([("createdAt", -1), ("_id", -1)])  # type: ignore[list-item]
        .limit(limit)
        .to_list()
    )
    return [_to_list_item_response(_to_domain(d)) for d in docs]


# ---------------------------------------------------------------------------
# Backtest API (RFC §10 + §13.1 gate 7 — retroactive backtest as trust unlock)
# ---------------------------------------------------------------------------


def _to_backtest_domain(doc: _ForesightBacktestDoc) -> BacktestRun:
    return BacktestRun(
        id=str(doc.id),
        workspace_id=doc.workspace,
        scenario_name=doc.scenario_name,
        status=doc.status,  # type: ignore[arg-type]
        created_at=doc.createdAt,
        request=dict(doc.request or {}),
        threshold=doc.threshold,
        result=dict(doc.result) if doc.result else None,
        gate_decision=dict(doc.gate_decision) if doc.gate_decision else None,
        error=doc.error,
        created_by=doc.created_by,
        updated_at=getattr(doc, "updatedAt", None),
    )


def _to_backtest_response(run: BacktestRun) -> BacktestRunResponse:
    return BacktestRunResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        scenario_name=run.scenario_name,
        status=run.status,
        created_at=iso_utc(run.created_at) or "",
        updated_at=iso_utc(run.updated_at),
        request=dict(run.request),
        threshold=run.threshold,
        result=dict(run.result) if run.result else None,
        gate_decision=dict(run.gate_decision) if run.gate_decision else None,
        error=run.error,
    )


def _to_backtest_list_item(run: BacktestRun) -> BacktestRunListItemResponse:
    return BacktestRunListItemResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        scenario_name=run.scenario_name,
        status=run.status,
        created_at=iso_utc(run.created_at) or "",
        updated_at=iso_utc(run.updated_at),
        threshold=run.threshold,
        gate_decision=dict(run.gate_decision) if run.gate_decision else None,
        error=run.error,
    )


def _to_gate_response(state: OnboardingGateState) -> OnboardingGateResponse:
    return OnboardingGateResponse(
        workspace_id=state.workspace_id,
        unlocked=state.unlocked,
        threshold=state.threshold,
        reason=state.reason,
        last_backtest_id=state.last_backtest_id,
        last_backtest_accuracy=state.last_backtest_accuracy,
        last_backtest_at=iso_utc(state.last_backtest_at),
    )


async def _fetch_backtest_in_workspace(
    workspace_id: str, backtest_id: str
) -> _ForesightBacktestDoc:
    """Fetch a backtest doc scoped to the caller's workspace.

    Same tenancy treatment as ``_fetch_in_workspace`` — malformed ids,
    missing docs, and cross-tenant ids all collapse to ``NotFound`` so
    existence isn't cross-tenant leakable.
    """
    try:
        oid = PydanticObjectId(backtest_id)
    except Exception:
        raise NotFound("foresight_backtest", backtest_id) from None
    doc = await _ForesightBacktestDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("foresight_backtest", backtest_id)
    return doc


def _resolve_threshold(requested: float | None) -> float:
    """Pick the effective threshold for one backtest run.

    v0.1: caller may tighten above ``GATE_DEFAULT_THRESHOLD`` but not
    relax below it — an operator who could relax the bar could trivially
    open the gate. v1.0's workspace-config override will let admins
    set the floor; the per-run tightening path stays.
    """
    if requested is None:
        return GATE_DEFAULT_THRESHOLD
    if requested < GATE_DEFAULT_THRESHOLD:
        raise ValidationError(
            "foresight.threshold_below_default",
            f"per-run threshold {requested} cannot relax below the default "
            f"{GATE_DEFAULT_THRESHOLD}; tighten above the floor only",
        )
    return requested


async def _score_backtest(
    body: CreateBacktestRequest,
    *,
    engine_result: dict[str, Any],
    threshold: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pair the engine's projected outcomes against the operator-supplied
    actual outcomes, aggregate, and score against the threshold.

    Returns ``(summary_wire_dict, gate_decision_wire_dict)``.

    v0.1 simulates the §10 pair-against-reality loop in-process — every
    anchor in ``body.anchors`` produces one ``CalibrationPair`` whose
    projected outcome comes from the engine's per-tick projection (when
    available) or a placeholder ``{}`` (the engine doesn't fan
    projections per anchor yet — PR 8 will). v1.0 wires the projection
    stream end-to-end via the ``projected_decisions`` collection.

    The aggregator imports are lazy so the cloud module stays clean of
    the engine layer per the import-linter contract.
    """
    from pocketpaw_ee.foresight.aggregator import (
        accuracy_meets_threshold,
        index_predictions,
    )
    from pocketpaw_ee.foresight.calibration import (
        aggregate_pairs,
        build_prediction_record,
        pair_against_reality,
    )

    # v0.1: synthesize one prediction per anchor using the engine's
    # modal projected outcome (single value across anchors for now —
    # PR 8 will pull per-anchor projections from the engine's per-tick
    # fanout). For the unlock gate's purpose, we only need pair counts
    # + per-pair match/mismatch flags; the actual projected payload can
    # be a placeholder while still exercising the aggregator path.
    projected_outcome_default: dict[str, Any] = {}
    # If the engine result happens to include a modal outcome (e.g. the
    # deterministic fake threads it through ``result["modal_outcome"]``),
    # pick that up so the pairing isn't degenerate. Otherwise stay
    # with an empty projection — the aggregator's missing-key delta
    # marker will simply count those as mismatches, which is what we
    # want when the engine hasn't fanned per-anchor projections yet.
    if isinstance(engine_result, dict):
        modal = engine_result.get("modal_outcome") or engine_result.get("projected_modal_outcome")
        if isinstance(modal, dict):
            projected_outcome_default = dict(modal)

    from datetime import UTC, datetime
    from uuid import uuid4

    run_id = uuid4()
    now = datetime.now(UTC)
    records = []
    pairs = []
    for anchor in body.anchors:
        record = build_prediction_record(
            scenario_template=anchor.scenario_template,
            run_id=run_id,
            anchor_object_id=anchor.anchor_object_id,
            projected_outcome=projected_outcome_default,
            observe_at=now,
            projection_confidence=anchor.projection_confidence,
        )
        records.append(record)
        pair = pair_against_reality(
            record,
            actual_outcome=dict(anchor.actual_outcome),
        )
        pairs.append(pair)

    summary = aggregate_pairs(
        pairs,
        predictions_by_id=index_predictions(records),
    )
    decision = accuracy_meets_threshold(summary, threshold=threshold)
    return summary.as_wire_dict(), decision.as_wire_dict()


async def create_backtest(ctx: RequestContext, body: CreateBacktestRequest) -> BacktestRunResponse:
    """Insert a backtest doc, drive the engine, score against the
    threshold, persist + emit. RFC §10 + §13.1 gate 7.

    Same three-write pattern as ``create_scenario_run`` (queued →
    running → complete/failed), but the ``complete`` transition also
    pins the aggregator's CalibrationSummary + ThresholdDecision into
    the doc so the gate label is stable across queries. When the
    decision passes, an extra ``ForesightOnboardingUnlocked`` event
    fires alongside ``ForesightBacktestCompleted`` so listeners can
    react to the gate flip specifically (the chat agent's onboarding
    skill watches for this).

    The per-run threshold is resolved against the workspace's effective
    floor (``GATE_DEFAULT_THRESHOLD`` in v0.1) — a request that tries
    to relax below the floor returns 422 before any persistence happens.
    """
    body = CreateBacktestRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    threshold = _resolve_threshold(body.threshold)

    # Surface engine-side scenario validation as 422 before opening a
    # doc row — the engine config carries the supported-sub-type list.
    try:
        from pocketpaw_ee.foresight.persona import OceanDrift
        from pocketpaw_ee.foresight.scenarios.runner import PersonaSpec, ScenarioConfig

        _ = ScenarioConfig(
            name=body.name,
            sub_type=body.sub_type,
            n_ticks=body.n_ticks,
            personas=[
                PersonaSpec(
                    name=p.name,
                    role=p.role,
                    ocean=OceanDrift(**p.ocean),
                )
                for p in body.personas
            ],
        )
    except (TypeError, ValueError, NotImplementedError) as exc:
        raise ValidationError("foresight.invalid_scenario", str(exc)) from exc

    doc = _ForesightBacktestDoc(
        workspace=workspace_id,
        scenario_name=body.name,
        status="queued",
        request=body.model_dump(),
        threshold=threshold,
        created_by=ctx.user_id,
    )
    await doc.insert()

    created_response = _to_backtest_response(_to_backtest_domain(doc))
    await emit(ForesightBacktestCreated(data=created_response.model_dump()))

    doc.status = "running"
    await doc.save()
    # no-event: ``running`` transition is the inline-run implementation
    # detail (matches ``create_scenario_run``'s convention). v1.0's
    # background-task migration emits a dedicated started event.

    try:
        # Reuse the scenario runner — anchors don't bind to engine
        # configuration in v0.1 (the engine doesn't fan per-anchor
        # projections yet; PR 8 wires that). The scoring step pairs the
        # engine's modal outcome against each anchor's actual_outcome.
        #
        # PR 8 (RFC 08 §8): backtests NEVER route to Instinct. The
        # explicit ``route_to_instinct=False`` belt-and-braces the
        # default — a future edit that flips the request DTO's default
        # to true must not silently turn the historical-replay path
        # into a proposal-spawning surface (the backtest is the trust
        # unlock, not an operator decision queue).
        scenario_body = CreateScenarioRequest(
            name=body.name,
            sub_type=body.sub_type,
            n_ticks=body.n_ticks,
            personas=body.personas,
            route_to_instinct=False,
        )
        engine_result = await _run_engine_inline(scenario_body)
        summary_dict, gate_dict = await _score_backtest(
            body,
            engine_result=engine_result,
            threshold=threshold,
        )
    except Exception as exc:  # noqa: BLE001 — capture into the doc, never bubble
        error_message = f"{type(exc).__name__}: {exc}"
        doc.status = "failed"
        doc.error = error_message
        await doc.save()
        failed_response = _to_backtest_response(_to_backtest_domain(doc))
        await emit(ForesightBacktestFailed(data=failed_response.model_dump()))
        logger.warning(
            "foresight.create_backtest: engine failed for backtest %s in ws=%s: %s",
            doc.id,
            workspace_id,
            error_message,
        )
        return failed_response

    # Combine the engine's run wire dict with the aggregator's
    # CalibrationSummary so a single ``result`` payload carries both
    # the per-run report and the scored accuracy.
    combined_result = dict(engine_result)
    combined_result["calibration_summary"] = summary_dict

    doc.status = "complete"
    doc.result = combined_result
    doc.gate_decision = gate_dict
    await doc.save()

    completed_response = _to_backtest_response(_to_backtest_domain(doc))
    await emit(ForesightBacktestCompleted(data=completed_response.model_dump()))

    # The gate-flip event fires only when this backtest passes — the
    # workspace's forward-sim posture just transitioned from closed (or
    # ambiguous) to open, and that's the signal the onboarding skill
    # is waiting on.
    if gate_dict.get("passed") is True:
        await emit(
            ForesightOnboardingUnlocked(
                data={
                    "workspace_id": workspace_id,
                    "backtest_id": completed_response.id,
                    "threshold": threshold,
                    "accuracy": gate_dict.get("observed"),
                }
            )
        )
    return completed_response


async def get_backtest(ctx: RequestContext, backtest_id: str) -> BacktestRunResponse:
    """Fetch a single backtest by id, scoped to the caller's workspace.

    Returns 404 (``foresight_backtest.not_found``) for unknown,
    malformed, or cross-tenant ids — same tenancy collapsing as the
    scenario-run path.
    """
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_backtest_in_workspace(workspace_id, backtest_id)
    # no-event: read-only path; emit only on writes (cloud rule #9).
    return _to_backtest_response(_to_backtest_domain(doc))


async def list_backtests(
    ctx: RequestContext, *, limit: int = 50
) -> list[BacktestRunListItemResponse]:
    """List backtests in the caller's workspace, most recent first.

    Same shape conventions as ``list_scenario_runs``: lighter list-item
    DTO that drops the inline result blob but keeps ``gate_decision`` so
    the Aggregate panel can render the unlock label per row without
    needing the detail endpoint.
    """
    workspace_id = _require_workspace(ctx)
    if limit < 1:
        raise ValidationError("foresight.invalid_limit", "limit must be >= 1")
    if limit > 200:
        limit = 200

    docs = (
        await _ForesightBacktestDoc.find({"workspace": workspace_id})
        .sort([("createdAt", -1), ("_id", -1)])  # type: ignore[list-item]
        .limit(limit)
        .to_list()
    )
    return [_to_backtest_list_item(_to_backtest_domain(d)) for d in docs]


async def get_onboarding_gate(ctx: RequestContext) -> OnboardingGateResponse:
    """Compose the workspace's onboarding gate state from the latest
    completed backtest. RFC §13.1 gate 7.

    Read-only — no emit (the unlock event is fired from
    ``create_backtest`` when the gate flips, not from the read path).

    Resolution rules:
      - No backtest in the workspace → ``unlocked=False, reason="no_backtest"``.
      - Latest completed backtest passed → ``unlocked=True, reason="unlocked"``.
      - Latest completed backtest failed → ``unlocked=False, reason="below_threshold"``.
      - Latest backtest is queued / running and no prior completed run
        exists → ``unlocked=False, reason="in_flight"``.
      - If a prior completed passing backtest exists, the gate stays
        unlocked even while a fresher backtest is in flight (the v1.0
        quarterly recalibration shouldn't briefly close the gate
        mid-run).

    The threshold echoed back is the workspace's effective floor
    (``GATE_DEFAULT_THRESHOLD`` in v0.1; v1.0 reads a workspace-config
    override here).
    """
    workspace_id = _require_workspace(ctx)
    threshold = GATE_DEFAULT_THRESHOLD

    latest_complete = await (
        _ForesightBacktestDoc.find({"workspace": workspace_id, "status": "complete"})
        .sort([("createdAt", -1), ("_id", -1)])  # type: ignore[list-item]
        .limit(1)
        .to_list()
    )

    if latest_complete:
        doc = latest_complete[0]
        gate = doc.gate_decision or {}
        passed = bool(gate.get("passed", False))
        observed = gate.get("observed")
        state = OnboardingGateState(
            workspace_id=workspace_id,
            unlocked=passed,
            threshold=threshold,
            reason="unlocked" if passed else "below_threshold",
            last_backtest_id=str(doc.id),
            last_backtest_accuracy=float(observed) if isinstance(observed, (int, float)) else None,
            last_backtest_at=doc.createdAt,
        )
        return _to_gate_response(state)

    # No completed backtest — check for in-flight before falling back to
    # "no_backtest". A queued/running backtest tells the UI to wait
    # rather than prompting the operator to start one from scratch.
    in_flight = await (
        _ForesightBacktestDoc.find(
            {"workspace": workspace_id, "status": {"$in": ["queued", "running"]}}
        )
        .sort([("createdAt", -1), ("_id", -1)])  # type: ignore[list-item]
        .limit(1)
        .to_list()
    )
    if in_flight:
        doc = in_flight[0]
        state = OnboardingGateState(
            workspace_id=workspace_id,
            unlocked=False,
            threshold=threshold,
            reason="in_flight",
            last_backtest_id=str(doc.id),
            last_backtest_accuracy=None,
            last_backtest_at=doc.createdAt,
        )
        return _to_gate_response(state)

    state = OnboardingGateState(
        workspace_id=workspace_id,
        unlocked=False,
        threshold=threshold,
        reason="no_backtest",
    )
    return _to_gate_response(state)


# ---------------------------------------------------------------------------
# Projected decisions (RFC §7.7 + PR 5 per-anchor projection fanout)
# ---------------------------------------------------------------------------


def _to_projected_decision_domain(doc: _ForesightProjectedDecisionDoc) -> ProjectedDecision:
    return ProjectedDecision(
        id=str(doc.id),
        workspace_id=doc.workspace,
        run_id=doc.run_id,
        anchor_id=doc.anchor_id,
        persona_id=doc.persona_id,
        tick_id=doc.tick_id,
        decision_text=doc.decision_text,
        confidence=doc.confidence,
        sub_type=doc.sub_type,
        forward_precedent_decision_id=doc.forward_precedent_decision_id,
        created_at=getattr(doc, "createdAt", None),
    )


def _to_projected_decision_response(pd: ProjectedDecision) -> ProjectedDecisionResponse:
    return ProjectedDecisionResponse(
        id=pd.id,
        workspace_id=pd.workspace_id,
        run_id=pd.run_id,
        anchor_id=pd.anchor_id,
        persona_id=pd.persona_id,
        tick_id=pd.tick_id,
        decision_text=pd.decision_text,
        confidence=pd.confidence,
        sub_type=pd.sub_type,
        forward_precedent_decision_id=pd.forward_precedent_decision_id,
        created_at=iso_utc(pd.created_at),
    )


async def emit_projected_decision(
    *,
    workspace_id: str,
    run_id: str,
    anchor_id: str,
    persona_id: str,
    tick_id: int,
    decision_text: str,
    confidence: float,
    sub_type: str,
    forward_precedent_decision_id: str | None = None,
    route_to_instinct: bool = False,
    scenario_name: str = "",
) -> ProjectedDecisionResponse:
    """Persist one projected-decision record and emit the event.

    Called by the engine's per-tick callback (wired in
    ``_run_engine_inline``). The callback is injected into the runner
    via closure so the engine module stays clean of the cloud import
    surface — the import-linter contract pins this direction.

    Tenancy:
      - ``workspace_id`` is required (the closure inside
        ``_run_engine_inline`` will only be wired when the cloud call
        has already resolved a workspace), so this function asserts
        rather than validating.
      - The run_id is the ForesightRun document id; cross-tenant
        protection is enforced by the run's own ``_fetch_in_workspace``
        check on the read side (a misrouted write here is impossible
        because the closure binds the workspace from the same
        RequestContext that constructed the run).

    Returns the persisted record as a response shape so callers can
    surface it on the live ws fan-out without a second round trip.

    Per RFC §7.7: ``forward_precedent_decision_id`` is stubbed ``None``
    until RFC 07 lands in pocketpaw; the field is part of the persisted
    shape so the backfill pass can populate it without a wire-shape
    bump.

    PR 8 (RFC 08 §8) — when ``route_to_instinct=True``, the function
    ALSO fans the projection into the Instinct approval queue via
    :func:`_fan_to_instinct_proposal`. The fan-out is idempotent — a
    re-emit of the same (workspace, run, tick, anchor, persona) bucket
    skips when a matching Instinct row already exists. The Instinct
    write is best-effort: a store failure logs a warning and never
    masks the projection write the engine is waiting on.
    """
    if not workspace_id:
        raise Forbidden(
            "foresight.no_workspace",
            "workspace required to emit a projected decision",
        )

    doc = _ForesightProjectedDecisionDoc(
        workspace=workspace_id,
        run_id=run_id,
        anchor_id=anchor_id,
        persona_id=persona_id,
        tick_id=tick_id,
        decision_text=decision_text,
        confidence=confidence,
        sub_type=sub_type,
        # v14 (§14.4) — caller threads the forward-precedent id resolved
        # by its DecisionGraphRef. ``None`` is still the default for
        # un-seeded scenarios; PR #1235 introduces synthetic ids only
        # when the scenario opts in via ``precedent_seed``.
        forward_precedent_decision_id=forward_precedent_decision_id,
    )
    await doc.insert()

    domain_pd = _to_projected_decision_domain(doc)
    response = _to_projected_decision_response(domain_pd)
    await emit(ForesightProjectedDecisionEmitted(data=response.model_dump()))

    # PR 8 (RFC 08 §8) — optional Instinct fan-out. Best-effort: a store
    # failure must never mask the projection write the engine is waiting
    # on. The Instinct rows live in OSS-runtime SQLite (``~/.pocketpaw/``)
    # so the lazy import here keeps the cloud module free of a static
    # ``pocketpaw.instinct`` dep at module top.
    if route_to_instinct:
        try:
            await _fan_to_instinct_proposal(
                domain_pd=domain_pd,
                scenario_name=scenario_name,
            )
        except Exception:  # noqa: BLE001 — never break the projection write
            logger.exception(
                "foresight.emit_projected_decision: Instinct fan-out failed "
                "for ws=%s run=%s anchor=%s tick=%s (non-fatal)",
                workspace_id,
                run_id,
                anchor_id,
                tick_id,
            )
    return response


async def list_projected_decisions(
    ctx: RequestContext,
    run_id: str,
    *,
    anchor_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ProjectedDecisionListResponse:
    """List projected decisions for a run, optionally filtered by anchor.

    Cross-tenant safety: this function calls ``_fetch_in_workspace``
    first so an unknown / cross-tenant run id surfaces as ``NotFound``
    *before* the projection query runs. That keeps the 404 collapsing
    rule consistent with ``get_scenario_run`` — existence is never
    leakable across tenants.

    Pagination:
      - ``limit`` defaults to 50; hard-capped at 500 so a misconfigured
        caller can't drag the entire collection into memory.
      - ``offset`` is the cursor; v0.5 keeps the cursor offset-based
        and computes ``total`` via ``count_documents`` under the same
        filter. v1.0 may swap to an opaque cursor once dataset sizes
        make ``count_documents`` expensive.
      - ``has_more`` is derived from ``offset + len(items) < total`` so
        callers can detect EOF without a second round trip.

    Order: ``(tick_id ASC, anchor_id ASC)`` matches the
    ``(workspace, run_id, tick_id, anchor_id)`` index so the query is a
    single bounded scan.
    """
    workspace_id = _require_workspace(ctx)
    # 404-collapse rule — run must exist in this workspace before the
    # projection query runs (otherwise an attacker could probe run-id
    # existence by listing projections that always return ``items=[]``).
    await _fetch_in_workspace(workspace_id, run_id)

    if limit < 1:
        raise ValidationError("foresight.invalid_limit", "limit must be >= 1")
    if limit > 500:
        limit = 500
    if offset < 0:
        raise ValidationError("foresight.invalid_offset", "offset must be >= 0")

    query: dict[str, Any] = {"workspace": workspace_id, "run_id": run_id}
    if anchor_id:
        query["anchor_id"] = anchor_id

    total = await _ForesightProjectedDecisionDoc.find(query).count()
    docs = (
        await _ForesightProjectedDecisionDoc.find(query)
        .sort([("tick_id", 1), ("anchor_id", 1)])  # type: ignore[list-item]
        .skip(offset)
        .limit(limit)
        .to_list()
    )
    items = [_to_projected_decision_response(_to_projected_decision_domain(d)) for d in docs]
    return ProjectedDecisionListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(items)) < total,
    )


# ---------------------------------------------------------------------------
# Foresight → Instinct approval loop (RFC 08 §8 + PR 8)
# ---------------------------------------------------------------------------
#
# The fan-out from a persisted ProjectedDecision into one Instinct
# proposal row. The cloud service owns this orchestration so the engine
# stays decoupled from Instinct and the import-linter's
# "engine → cloud forbidden" contract holds. The bridge module
# (``ee.foresight.instinct_bridge``) is pure conversion — no Beanie,
# no store — and the heavy lifting (read-before-write idempotence,
# event emission) lives here.


_FORESIGHT_POCKET_PREFIX = "foresight:run:"


def _foresight_pocket_id(run_id: str) -> str:
    """Stable synthetic ``pocket_id`` for an Instinct row spawned by a
    Foresight run. The Instinct store treats ``pocket_id`` as a
    free-form string (no FK) so a prefix-scoped query recovers every
    row a single run produced.
    """
    return f"{_FORESIGHT_POCKET_PREFIX}{run_id}" if run_id else f"{_FORESIGHT_POCKET_PREFIX}unknown"


async def _existing_dedupe_keys(store: Any, pocket_id: str) -> set[str]:
    """Read the dedupe keys already stamped on Instinct rows for one
    Foresight run.

    Returns a set so the idempotence check is O(1) per projection
    when the fan-out replays a long run. Rows without the
    ``_foresight.dedupe_key`` field (e.g. a hand-crafted Action that
    happens to land in the same pocket-id namespace) are skipped —
    we only dedupe against our own provenance block.
    """
    actions = await store.list_actions(pocket_id=pocket_id, limit=500)
    keys: set[str] = set()
    for act in actions:
        params = getattr(act, "parameters", {}) or {}
        block = params.get("_foresight") if isinstance(params, dict) else None
        if isinstance(block, dict):
            key = block.get("dedupe_key")
            if isinstance(key, str) and key:
                keys.add(key)
    return keys


async def _fan_to_instinct_proposal(
    *,
    domain_pd: ProjectedDecision,
    scenario_name: str,
) -> str | None:
    """Spawn one Instinct ``Action`` row from a ProjectedDecision.

    The conversion is delegated to
    :func:`ee.foresight.instinct_bridge.projected_decision_to_instinct_proposal`
    (pure conversion, no store call). This function adds the
    cloud-side wiring:

      1. Resolve the synthetic ``pocket_id`` for the run.
      2. Read existing Instinct rows in that pocket scope and skip if
         a row with the same dedupe key already exists (idempotence).
      3. Build the ``ActionTrigger`` (the bridge stays string-typed so
         the engine namespace doesn't pull ``pocketpaw.instinct``).
      4. Call ``store.propose(...)``.
      5. Emit ``ForesightInstinctProposalCreated``.

    Returns the spawned Action id on success, or ``None`` when the
    fan-out was skipped (duplicate) or silently no-oped (no run id).

    Imports for the Instinct surface are lazy at function scope so
    importing the cloud module stays cheap (no SQLite touch, no
    OSS-runtime side effects) until the fan-out actually fires.
    """
    from pocketpaw.instinct.models import (
        ActionCategory,
        ActionPriority,
        ActionTrigger,
    )
    from pocketpaw.stores import get_instinct_store
    from pocketpaw_ee.foresight.instinct_bridge import (
        projected_decision_to_instinct_proposal,
    )

    proposal = projected_decision_to_instinct_proposal(
        domain_pd,
        scenario_config={"name": scenario_name} if scenario_name else None,
    )
    dedupe_key = proposal.parameters.get("_foresight", {}).get("dedupe_key", "")

    store = get_instinct_store()
    existing = await _existing_dedupe_keys(store, proposal.pocket_id)
    if dedupe_key in existing:
        # Idempotent skip — a re-emit of the same (ws, run, tick,
        # anchor, persona) bucket already has a row in The Tray.
        logger.debug(
            "foresight._fan_to_instinct_proposal: skipped duplicate dedupe_key=%s",
            dedupe_key,
        )
        return None

    trigger = ActionTrigger(
        type=proposal.trigger_type,
        source=proposal.trigger_source,
        reason=proposal.trigger_reason,
    )
    # Pydantic enum coercion — the bridge stays string-typed so the
    # engine namespace doesn't drag in the Instinct domain at module
    # top; the store call needs the real enums.
    try:
        category = ActionCategory(proposal.category)
    except ValueError:
        category = ActionCategory.DATA
    try:
        priority = ActionPriority(proposal.priority)
    except ValueError:
        priority = ActionPriority.MEDIUM

    action = await store.propose(
        pocket_id=proposal.pocket_id,
        title=proposal.title,
        description=proposal.description,
        recommendation=proposal.recommendation,
        trigger=trigger,
        category=category,
        priority=priority,
        parameters=proposal.parameters,
        assignee=proposal.assignee,
    )

    await emit(
        ForesightInstinctProposalCreated(
            data={
                "action_id": action.id,
                "pocket_id": proposal.pocket_id,
                "workspace_id": domain_pd.workspace_id,
                "run_id": domain_pd.run_id,
                "tick_id": domain_pd.tick_id,
                "anchor_id": domain_pd.anchor_id,
                "persona_id": domain_pd.persona_id,
                "sub_type": domain_pd.sub_type,
                "confidence": domain_pd.confidence,
                "dedupe_key": dedupe_key,
            }
        )
    )
    return action.id


def _instinct_action_to_response(action: Any) -> ForesightInstinctProposalResponse:
    """Convert a ``pocketpaw.instinct.models.Action`` (or any duck-typed
    equivalent) into the Foresight-flavoured response shape.

    Duck-typed so test doubles can hand in a ``SimpleNamespace`` without
    importing the Instinct domain module from cloud-test code.
    """
    params = getattr(action, "parameters", {}) or {}
    block = params.get("_foresight") if isinstance(params, dict) else {}
    if not isinstance(block, dict):
        block = {}
    created_at = getattr(action, "created_at", None)
    return ForesightInstinctProposalResponse(
        action_id=str(getattr(action, "id", "")),
        pocket_id=str(getattr(action, "pocket_id", "")),
        title=str(getattr(action, "title", "")),
        description=str(getattr(action, "description", "")),
        recommendation=str(getattr(action, "recommendation", "")),
        status=getattr(getattr(action, "status", None), "value", "pending"),
        priority=getattr(getattr(action, "priority", None), "value", "medium"),
        category=getattr(getattr(action, "category", None), "value", "data"),
        assignee=getattr(action, "assignee", None),
        created_at=iso_utc(created_at) if created_at is not None else None,
        foresight=block,
    )


async def list_instinct_proposals_for_run(
    ctx: RequestContext,
    run_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> ForesightInstinctProposalListResponse:
    """List the Instinct proposals spawned by one Foresight run.

    Reads the OSS-runtime Instinct store filtered to the run's
    synthetic ``pocket_id`` (``foresight:run:<run_id>``) and returns
    the rows whose ``parameters._foresight.run_id`` matches.

    Cross-tenant safety: this function calls ``_fetch_in_workspace``
    first so an unknown / cross-tenant run id surfaces as ``NotFound``
    *before* the Instinct query runs. That keeps the 404-collapse rule
    consistent with the projection-list endpoint — existence is never
    leakable across tenants. The Instinct store itself does not carry
    a ``workspace_id`` column (it's OSS-runtime SQLite), so the
    workspace check has to happen at the run level here.

    Pagination is offset-based for parity with the projection-list
    endpoint. ``total`` is computed locally over the pocket-scoped
    list because Instinct doesn't expose a count surface; this is
    cheap until a single run accumulates thousands of projections,
    at which point v1.0 will swap to a streaming reader.
    """
    workspace_id = _require_workspace(ctx)
    # 404-collapse rule — the run must exist in this workspace before
    # any Instinct read runs. Without this, the Instinct store (which
    # doesn't carry workspace_id) would happily return rows even for
    # a cross-tenant run-id probe.
    await _fetch_in_workspace(workspace_id, run_id)

    if limit < 1:
        raise ValidationError("foresight.invalid_limit", "limit must be >= 1")
    if limit > 500:
        limit = 500
    if offset < 0:
        raise ValidationError("foresight.invalid_offset", "offset must be >= 0")

    # Lazy import — the Instinct store is OSS-runtime SQLite; importing
    # at module top would touch the disk on every cloud module load.
    from pocketpaw.stores import get_instinct_store

    store = get_instinct_store()
    pocket_id = _foresight_pocket_id(run_id)
    # Pull a generous slice (Instinct's max page size is 500) and
    # filter to the rows our own provenance stamped, in case a future
    # caller drops an Action into the same pocket-id namespace by hand.
    raw = await store.list_actions(pocket_id=pocket_id, limit=500)
    matching = [
        a
        for a in raw
        if isinstance(getattr(a, "parameters", None), dict)
        and isinstance(a.parameters.get("_foresight"), dict)
        and a.parameters["_foresight"].get("run_id") == run_id
    ]
    total = len(matching)
    page = matching[offset : offset + limit]
    items = [_instinct_action_to_response(a) for a in page]
    return ForesightInstinctProposalListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(items)) < total,
    )


# ---------------------------------------------------------------------------
# Scenario catalog (RFC §11.2) — bundled YAML template enumeration.
#
# Read-only, workspace-agnostic — the catalog enumerates the engine's
# bundled scenarios. The license dep stays on the route so OSS-only
# consumers don't pull the catalog through the cloud surface.
# ---------------------------------------------------------------------------


_SCENARIOS_CATALOG: list[ScenarioCatalogEntry] | None = None
_SCENARIOS_CATALOG_MTIME: float | None = None
_SCENARIO_DESCRIPTIONS: dict[str, str] = {
    "decision_forecast": (
        "Rehearse a single approval-style decision across a small "
        "persona panel — the fastest end-to-end loop. One tick, modal "
        "outcome aggregated across the cohort."
    ),
    "market_sim": (
        "Project market-segment behaviour over a short horizon — "
        "enterprise / SMB / channel segments react to a pricing or "
        "competitor shift declared on the scenario YAML."
    ),
    "org_change_rehearsal": (
        "Walk a multi-step rollout (announce → training → deadline → "
        "escalation) through a mixed manager / IC / ops cohort and "
        "score per-event adoption + resistance."
    ),
}


def _scenarios_dir():
    """Resolve the bundled scenarios directory on disk.

    Returns a :class:`pathlib.Path`. Type annotation is intentionally
    omitted so the cloud module doesn't pick up a static
    ``pocketpaw_ee.foresight.scenarios`` import — the package's
    ``__file__`` is read lazily here only when the catalog rebuilds.
    """
    from pathlib import Path

    # Module path resolution stays lazy so the import-linter's
    # "engine import only inside functions" contract holds — the
    # scenarios package is OSS-light (just YAML files + a small loader),
    # but the principle is uniform across this module.
    import pocketpaw_ee.foresight.scenarios as _scenarios_pkg

    pkg_path = Path(_scenarios_pkg.__file__).parent
    return pkg_path


def _build_catalog_entries() -> list[ScenarioCatalogEntry]:
    """Walk the scenarios directory and produce one catalog entry per
    YAML file. Pure / read-only.

    Each YAML is parsed with ``yaml.safe_load`` — no engine module
    imports happen (persona, LLM, substrate stay untouched). The
    ``id`` is the YAML stem; for the three v0.5-shipped templates
    that's also the ``sub_type`` field.
    """
    import yaml  # type: ignore[import-untyped]  # noqa: PLC0415

    pkg_dir = _scenarios_dir()
    entries: list[ScenarioCatalogEntry] = []
    # Sort by stem so the response order is deterministic across
    # filesystems (the YAML on-disk sort drives the picker order).
    for path in sorted(pkg_dir.glob("*.yaml")):
        try:
            with open(path) as fh:
                data = yaml.safe_load(fh) or {}
        except Exception:  # noqa: BLE001 — skip malformed YAML; never break the listing
            logger.exception("foresight.list_scenarios: failed to parse %s", path)
            continue
        if not isinstance(data, dict):
            continue
        stem = path.stem
        sub_type = str(data.get("sub_type") or stem)
        name = str(data.get("name") or stem)
        personas = data.get("personas") or []
        n_ticks = int(data.get("n_ticks") or 0)
        tier_mix_block = data.get("tier_mix") or {}
        tier_mix: dict[str, float] = {}
        if isinstance(tier_mix_block, dict):
            for tier_name in ("premium", "mid", "tail"):
                if tier_name in tier_mix_block:
                    try:
                        tier_mix[tier_name] = float(tier_mix_block[tier_name])
                    except (TypeError, ValueError):
                        continue
        # If the YAML omitted tier_mix, echo the captain-locked default.
        if not tier_mix:
            tier_mix = {"premium": 0.05, "mid": 0.15, "tail": 0.80}
        description = _SCENARIO_DESCRIPTIONS.get(
            sub_type,
            "Foresight scenario template (RFC 08 §4).",
        )
        entries.append(
            ScenarioCatalogEntry(
                id=stem,
                name=name,
                sub_type=sub_type,
                description=description,
                num_personas=len(personas) if isinstance(personas, list) else 0,
                num_ticks=n_ticks,
                tier_mix=tier_mix,
            )
        )
    return entries


def _get_catalog(force_reload: bool = False) -> list[ScenarioCatalogEntry]:
    """Return the cached catalog, rebuilding if the directory mtime
    changed (touch-to-reload). Tests can force a reload via the kwarg.
    """
    global _SCENARIOS_CATALOG, _SCENARIOS_CATALOG_MTIME

    try:
        current_mtime = _scenarios_dir().stat().st_mtime
    except Exception:  # noqa: BLE001 — fall through to the empty catalog
        current_mtime = None

    if force_reload or _SCENARIOS_CATALOG is None or _SCENARIOS_CATALOG_MTIME != current_mtime:
        _SCENARIOS_CATALOG = _build_catalog_entries()
        _SCENARIOS_CATALOG_MTIME = current_mtime
    return _SCENARIOS_CATALOG


async def list_scenarios(_ctx: RequestContext) -> ScenarioCatalogResponse:
    """Return the bundled scenario template catalog.

    Workspace-agnostic — the catalog is a global static enumeration of
    the engine's bundled YAML files; v1.0 may add workspace-owned
    scenarios (RFC §18 grammar). The context arg is accepted for
    consistency with the other service functions and to keep the
    route signature uniform.
    """
    entries = _get_catalog()
    items = [
        ScenarioCatalogItem(
            id=e.id,
            name=e.name,
            sub_type=e.sub_type,
            description=e.description,
            num_personas=e.num_personas,
            num_ticks=e.num_ticks,
            tier_mix=dict(e.tier_mix),
        )
        for e in entries
    ]
    return ScenarioCatalogResponse(items=items)


# ---------------------------------------------------------------------------
# Aggregate rollup (RFC §11.5) — rolling accuracy + drift + modal dist.
#
# Reads the workspace's persisted backtest summaries + projected
# decisions over the window. PR 4's calibration buffer didn't land a
# Mongo collection for ``PredictionRecord``; the rollup proxies the
# missing data via the backtest summaries (rolling accuracy +
# confidence drift) and the projection collection (modal outcome
# distribution). Documented limitation; v1.0 wires the real buffer.
# ---------------------------------------------------------------------------


AGGREGATE_DEFAULT_WINDOW_DAYS: int = 30
AGGREGATE_MAX_WINDOW_DAYS: int = 90


def _resolve_window_days(window_days: int | None) -> int:
    """Resolve the request's window_days against the v0.1 limits.

    Returns the canonical window in days (default 30). Above 90 raises
    a ValidationError (422) — the UI lead's TypeScript shape locks
    the cap at 90 so a 91 from a misconfigured client must surface as
    a structured error, not a silent clamp.
    """
    if window_days is None:
        return AGGREGATE_DEFAULT_WINDOW_DAYS
    if window_days < 1:
        raise ValidationError(
            "foresight.invalid_window",
            f"window_days must be >= 1, got {window_days}",
        )
    if window_days > AGGREGATE_MAX_WINDOW_DAYS:
        raise ValidationError(
            "foresight.invalid_window",
            f"window_days must be <= {AGGREGATE_MAX_WINDOW_DAYS}, got {window_days}",
        )
    return window_days


def _compose_rolling_accuracy(
    backtest_docs: list[Any],
    *,
    window_days: int,
    now: datetime,
) -> tuple[RollingAccuracyPoint, ...]:
    """Bucket completed backtests by day and emit one point per bucket.

    ``accuracy`` is the per-bucket mean of ``gate_decision.observed``
    across the backtests that completed in the bucket; ``sample_count``
    is the per-bucket backtest count (i.e. the proxy for prediction
    pairs since PR 4's buffer is in-memory only). Empty buckets are
    omitted from the points array — the UI's chart fills gaps from
    the bucket end-timestamp grid.
    """
    buckets: dict[datetime, list[tuple[float, int]]] = {}
    for doc in backtest_docs:
        gate = doc.gate_decision or {}
        observed = gate.get("observed")
        if not isinstance(observed, (int, float)):
            continue
        ts = getattr(doc, "createdAt", None) or now
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC_TZ)
        # Bucket at day boundary (UTC) so the wire ts is stable.
        bucket_ts = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        n_pairs = int(gate.get("n_pairs") or 0)
        buckets.setdefault(bucket_ts, []).append((float(observed), max(n_pairs, 1)))

    points: list[RollingAccuracyPoint] = []
    for ts, entries in sorted(buckets.items()):
        if not entries:
            continue
        total_samples = sum(sc for _, sc in entries)
        # Weighted average across the bucket so a backtest with 100
        # anchors counts proportionally more than one with 5.
        weighted = sum(acc * sc for acc, sc in entries)
        avg_accuracy = weighted / total_samples if total_samples else 0.0
        points.append(
            RollingAccuracyPoint(
                ts=ts,
                accuracy=round(max(0.0, min(1.0, avg_accuracy)), 4),
                sample_count=total_samples,
            )
        )
    return tuple(points)


def _compose_confidence_drift(
    backtest_docs: list[Any],
    *,
    flat_threshold: float = 0.05,
) -> ConfidenceDrift:
    """Compute the §11.5 confidence-drift summary.

    Compares the earliest vs latest completed backtest's
    ``result.calibration_summary.confidence_calibration``. Empty input
    returns a flat zero-magnitude record so the UI can render "no
    data" without a separate path.

    Vocabulary:
      - ``rising``   — calibration improved (delta > flat_threshold)
      - ``falling``  — calibration degraded (delta < -flat_threshold)
      - ``flat``     — within the flat band (or no data)
    """
    if not backtest_docs:
        return ConfidenceDrift(trend="flat", magnitude=0.0)
    confidences: list[tuple[datetime, float]] = []
    for doc in backtest_docs:
        result = doc.result or {}
        summary = result.get("calibration_summary") if isinstance(result, dict) else None
        if not isinstance(summary, dict):
            continue
        conf = summary.get("confidence_calibration")
        if not isinstance(conf, (int, float)):
            continue
        ts = getattr(doc, "createdAt", None)
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC_TZ)
        if ts is None:
            continue
        confidences.append((ts, float(conf)))
    if len(confidences) < 2:
        # Single point — can't compute drift. Flat zero so consumers
        # can render the empty state.
        return ConfidenceDrift(trend="flat", magnitude=0.0)
    confidences.sort(key=lambda item: item[0])
    earliest = confidences[0][1]
    latest = confidences[-1][1]
    delta = latest - earliest
    magnitude = abs(delta)
    trend: Literal["rising", "falling", "flat"]
    if magnitude < flat_threshold:
        trend = "flat"
    elif delta > 0:
        trend = "rising"
    else:
        trend = "falling"
    return ConfidenceDrift(trend=trend, magnitude=round(magnitude, 4))


def _compose_modal_outcome_distribution(
    projection_docs: list[Any],
) -> tuple[ModalOutcomeEntry, ...]:
    """Tally projected-decision modal verbs into a normalised share map.

    Each :class:`ForesightProjectedDecision` carries the modal verb
    (``decision_text``); the distribution counts each unique verb and
    normalises by the total record count. Empty input yields an empty
    tuple.
    """
    counts: dict[str, int] = {}
    for doc in projection_docs:
        verb = (getattr(doc, "decision_text", "") or "").strip().lower()
        if not verb:
            continue
        counts[verb] = counts.get(verb, 0) + 1
    total = sum(counts.values())
    if total == 0:
        return ()
    entries = [
        ModalOutcomeEntry(
            outcome=outcome,
            share=round(count / total, 4),
        )
        for outcome, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return tuple(entries)


async def _fetch_aggregate_inputs(
    workspace_id: str,
    *,
    window_days: int,
    now: datetime,
) -> tuple[list[Any], list[Any]]:
    """Read the workspace's persisted backtests + projection records
    over the window. Returns ``(backtest_docs, projection_docs)``.

    Tenant filter on every read per cloud rule #7. ``createdAt``
    comparison uses a UTC-anchored lower bound so the window is
    deterministic across deployments with different default tzs.
    """
    window_start = now - timedelta(days=window_days)
    backtest_docs = await (
        _ForesightBacktestDoc.find(
            {
                "workspace": workspace_id,
                "status": "complete",
                "createdAt": {"$gte": window_start},
            }
        )
        .sort([("createdAt", 1), ("_id", 1)])  # type: ignore[list-item]
        .to_list()
    )
    projection_docs = await _ForesightProjectedDecisionDoc.find(
        {
            "workspace": workspace_id,
            "createdAt": {"$gte": window_start},
        }
    ).to_list()
    return backtest_docs, projection_docs


def _aggregate_to_response(rollup: AggregateRollup) -> AggregateRollupResponse:
    """Map the frozen-dataclass rollup into the wire shape.

    Done by hand rather than ``model_validate(from_attributes=True)``
    because the tuple-of-domain → list-of-pydantic projection is more
    explicit than relying on Pydantic's nested coercion + every nested
    DTO uses ``extra='forbid'``.
    """
    return AggregateRollupResponse(
        window_days=rollup.window_days,
        generated_at=_iso_required(rollup.generated_at),
        rolling_accuracy=RollingAccuracySeriesDto(
            points=[
                RollingAccuracyPointDto(
                    ts=_iso_required(p.ts),
                    accuracy=p.accuracy,
                    sample_count=p.sample_count,
                )
                for p in rollup.rolling_accuracy
            ]
        ),
        confidence_drift=ConfidenceDriftDto(
            trend=rollup.confidence_drift.trend,
            magnitude=rollup.confidence_drift.magnitude,
        ),
        modal_outcome_distribution=ModalOutcomeDistributionDto(
            entries=[
                ModalOutcomeEntryDto(
                    outcome=e.outcome,
                    share=e.share,
                )
                for e in rollup.modal_outcome_distribution
            ]
        ),
    )


def _iso_required(dt: datetime) -> str:
    """Like :func:`iso_utc` but never returns ``None`` — used when the
    wire field is non-optional. Naïve datetimes are re-anchored to UTC
    so the response always ends in ``Z``.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(UTC_TZ).isoformat().replace("+00:00", "Z")


async def get_aggregate_rollup(
    ctx: RequestContext,
    *,
    window_days: int | None = None,
) -> AggregateRollupResponse:
    """Compose the workspace's aggregate rollup over the trailing window.

    Read-only — no event emit, no Beanie writes. Empty workspaces
    collapse to zeros + empty arrays so the UI never sees a 404 on
    new-tenant boot. Above the 90-day cap → 422
    ``foresight.invalid_window`` (validated in :func:`_resolve_window_days`).
    """
    workspace_id = _require_workspace(ctx)
    effective_window = _resolve_window_days(window_days)
    now = datetime.now(UTC_TZ)

    backtest_docs, projection_docs = await _fetch_aggregate_inputs(
        workspace_id,
        window_days=effective_window,
        now=now,
    )

    rolling = _compose_rolling_accuracy(
        backtest_docs,
        window_days=effective_window,
        now=now,
    )
    drift = _compose_confidence_drift(backtest_docs)
    distribution = _compose_modal_outcome_distribution(projection_docs)

    rollup = AggregateRollup(
        workspace_id=workspace_id,
        window_days=effective_window,
        generated_at=now,
        rolling_accuracy=rolling,
        confidence_drift=drift,
        modal_outcome_distribution=distribution,
    )
    return _aggregate_to_response(rollup)


# ---------------------------------------------------------------------------
# Insights (RFC §11.6) — five-rule pattern synthesizer over the aggregate.
#
# The synthesizer module (``ee.foresight.insights``) lives in the engine
# namespace so the rule logic stays pure / no I/O / no Beanie. The
# cloud service composes the input bundle from the same persistence
# the aggregate endpoint reads, then delegates the rule evaluation.
# Engine import is lazy per the import-linter contract.
# ---------------------------------------------------------------------------


INSIGHTS_DEFAULT_CAP: int = 20


def _compose_per_persona_calibration(
    projection_docs: list[Any],
    *,
    floor_threshold: float,
) -> list[Any]:
    """Build the per-persona calibration proxy the synthesizer reads.

    PR 4's CalibrationPair persistence didn't ship — the rule fires
    on per-persona confidence levels read off the projection
    collection as a proxy until the real buffer migrates to Mongo.

    The synthesizer's ``PerPersonaCalibration`` dataclass lives in
    the engine namespace; we import it lazily inside the function so
    the cloud module stays clean of the engine layer at module top.
    """
    from pocketpaw_ee.foresight.insights import PerPersonaCalibration

    per_persona: dict[str, list[float]] = {}
    for doc in projection_docs:
        persona_id = getattr(doc, "persona_id", "") or ""
        if not persona_id:
            continue
        confidence = getattr(doc, "confidence", None)
        if not isinstance(confidence, (int, float)):
            continue
        per_persona.setdefault(persona_id, []).append(float(confidence))

    out: list[Any] = []
    for persona_id, confidences in per_persona.items():
        if not confidences:
            continue
        mean_conf = sum(confidences) / len(confidences)
        # Synthesizer threshold gating happens in the rule itself;
        # we surface every persona we have data for so a follow-up
        # rule (or v1.0 LLM synthesizer) can re-evaluate without a
        # second fetch.
        # Drop personas with samples below 2 to avoid noise from a
        # single low projection.
        if len(confidences) < 2:
            continue
        out.append(
            PerPersonaCalibration(
                persona_id=persona_id,
                calibration=round(mean_conf, 4),
                sample_count=len(confidences),
            )
        )
    # Keep deterministic ordering so the synthesizer's output is
    # stable across re-fetches.
    out.sort(key=lambda entry: entry.persona_id)
    return out


def _compose_tier_distribution_deltas(
    backtest_docs: list[Any],
    *,
    configured_default: dict[str, float],
) -> list[Any]:
    """Compute the configured-vs-actual tier mix delta per tier.

    The actual mix is averaged across the workspace's recent
    completed backtests (the engine reports ``tier_distribution``
    per run on ``result.tier_distribution``). Configured comes from
    the locked default — v1.0 will read a per-scenario override from
    the request when the operator opted in.

    Returns a list of :class:`TierDistributionDelta` instances
    suitable for the synthesizer's ``tier_distribution_deltas``
    bundle field. Empty input yields an empty list (no tier-imbalance
    insights fire).
    """
    from pocketpaw_ee.foresight.insights import TierDistributionDelta

    actual_counts: dict[str, int] = {}
    total = 0
    for doc in backtest_docs:
        result = doc.result or {}
        if not isinstance(result, dict):
            continue
        tier_dist = result.get("tier_distribution")
        if not isinstance(tier_dist, dict):
            continue
        for tier, count in tier_dist.items():
            if not isinstance(count, int):
                continue
            actual_counts[str(tier)] = actual_counts.get(str(tier), 0) + count
            total += count
    if total == 0:
        return []
    deltas: list[Any] = []
    for tier, configured in configured_default.items():
        actual = actual_counts.get(tier, 0) / total if total else 0.0
        deltas.append(
            TierDistributionDelta(
                tier=tier,
                configured=round(float(configured), 4),
                actual=round(actual, 4),
            )
        )
    return deltas


def _compose_latest_backtest_gate(
    backtest_docs: list[Any],
) -> Any | None:
    """Build the synthesizer's ``LatestBacktestGate`` from the freshest
    completed backtest. Returns ``None`` when no backtests exist —
    the threshold_unmet rule then silently skips.
    """
    from pocketpaw_ee.foresight.insights import LatestBacktestGate

    if not backtest_docs:
        return None
    # Backtest docs already arrive sorted ascending by createdAt from
    # ``_fetch_aggregate_inputs``; the freshest is the last entry.
    doc = backtest_docs[-1]
    gate = doc.gate_decision or {}
    observed = gate.get("observed")
    threshold = gate.get("threshold")
    passed = bool(gate.get("passed", False))
    if not isinstance(observed, (int, float)) or not isinstance(threshold, (int, float)):
        # Malformed gate payload — skip rather than fire a bogus insight.
        return None
    completed_at = getattr(doc, "createdAt", None)
    if isinstance(completed_at, datetime) and completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC_TZ)
    return LatestBacktestGate(
        backtest_id=str(doc.id),
        passed=passed,
        observed=float(observed),
        threshold=float(threshold),
        completed_at=completed_at,
    )


def _insight_to_response(view: InsightView) -> InsightResponse:
    """Map the domain :class:`InsightView` into the wire shape.

    Tuple → list conversion happens here so the DTO carries a plain
    list. ``generated_at`` is converted to ISO-8601 UTC with a Z
    suffix for parity with the rest of the foresight wire surface.
    """
    return InsightResponse(
        id=view.id,
        kind=view.kind,
        title=view.title,
        body=view.body,
        severity=view.severity,
        anchor_refs=list(view.anchor_refs),
        generated_at=_iso_required(view.generated_at),
    )


async def get_insights(ctx: RequestContext) -> InsightsResponse:
    """Compose the workspace's Insights panel response.

    Composes the synthesizer input bundle from the same aggregate
    inputs the rollup endpoint reads, then delegates rule evaluation
    to :func:`ee.foresight.insights.synthesize_insights`. Engine
    import is lazy so the cloud module stays clean of the engine
    layer per the import-linter contract.

    Empty workspaces return ``items=[]`` — the synthesizer yields no
    rows when none of the five rules can fire.
    """
    workspace_id = _require_workspace(ctx)
    now = datetime.now(UTC_TZ)

    # Reuse the aggregate window so the insights view is consistent
    # with the rollup view the UI renders side-by-side.
    backtest_docs, projection_docs = await _fetch_aggregate_inputs(
        workspace_id,
        window_days=AGGREGATE_DEFAULT_WINDOW_DAYS,
        now=now,
    )

    rolling = _compose_rolling_accuracy(
        backtest_docs,
        window_days=AGGREGATE_DEFAULT_WINDOW_DAYS,
        now=now,
    )
    drift = _compose_confidence_drift(backtest_docs)
    per_persona = _compose_per_persona_calibration(
        projection_docs,
        floor_threshold=0.50,
    )
    tier_deltas = _compose_tier_distribution_deltas(
        backtest_docs,
        configured_default={"premium": 0.05, "mid": 0.15, "tail": 0.80},
    )
    latest_gate = _compose_latest_backtest_gate(backtest_docs)

    # Lazy import — the synthesizer module is pure but lives in the
    # engine namespace, so the import-linter forbids static imports
    # from the cloud surface. Imported inside the function call
    # path keeps the contract clean.
    from pocketpaw_ee.foresight.insights import (  # noqa: PLC0415
        ConfidenceDriftInput,
        SynthesizerInput,
        synthesize_insights,
    )
    from pocketpaw_ee.foresight.insights import (
        RollingAccuracyPoint as _RollingPoint,
    )

    bundle = SynthesizerInput(
        now=now,
        rolling_accuracy=tuple(
            _RollingPoint(
                ts=p.ts,
                accuracy=p.accuracy,
                sample_count=p.sample_count,
            )
            for p in rolling
        ),
        confidence_drift=ConfidenceDriftInput(
            trend=drift.trend,
            magnitude=drift.magnitude,
        ),
        per_persona_calibration=tuple(per_persona),
        tier_distribution_deltas=tuple(tier_deltas),
        latest_backtest=latest_gate,
    )

    raw_insights = synthesize_insights(bundle, cap=INSIGHTS_DEFAULT_CAP)
    items = [
        _insight_to_response(
            InsightView(
                id=insight.id,
                kind=insight.kind,
                title=insight.title,
                body=insight.body,
                severity=insight.severity,
                anchor_refs=insight.anchor_refs,
                generated_at=insight.generated_at,
            )
        )
        for insight in raw_insights
    ]
    return InsightsResponse(items=items)


__all__ = [
    "AGGREGATE_DEFAULT_WINDOW_DAYS",
    "AGGREGATE_MAX_WINDOW_DAYS",
    "GATE_DEFAULT_THRESHOLD",
    "INSIGHTS_DEFAULT_CAP",
    "create_backtest",
    "create_scenario_run",
    "emit_projected_decision",
    "get_aggregate_rollup",
    "get_backtest",
    "get_insights",
    "get_onboarding_gate",
    "get_scenario_run",
    "list_backtests",
    "list_instinct_proposals_for_run",
    "list_projected_decisions",
    "list_scenario_runs",
    "list_scenarios",
]
