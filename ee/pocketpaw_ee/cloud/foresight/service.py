# ee/pocketpaw_ee/cloud/foresight/service.py
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
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    ForesightBacktestCompleted,
    ForesightBacktestCreated,
    ForesightBacktestFailed,
    ForesightOnboardingUnlocked,
    ForesightRunCompleted,
    ForesightRunCreated,
    ForesightRunFailed,
)
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.foresight.domain import (
    BacktestRun,
    OnboardingGateState,
    ScenarioRun,
)
from pocketpaw_ee.cloud.foresight.dto import (
    BacktestRunListItemResponse,
    BacktestRunResponse,
    CreateBacktestRequest,
    CreateScenarioRequest,
    OnboardingGateResponse,
    ScenarioRunListItemResponse,
    ScenarioRunResponse,
)
from pocketpaw_ee.cloud.models.foresight_backtest import (
    ForesightBacktest as _ForesightBacktestDoc,
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


async def _run_engine_inline(body: CreateScenarioRequest) -> dict[str, Any]:
    """Drive the foresight engine for one scenario run.

    Imports are lazy: the engine modules (persona, llm, scenarios) live
    under ``ee.pocketpaw_ee.foresight.*`` and the cloud layer must not
    statically import them per the cloud-vs-engine separation. The
    cloud rule #2 forbids touching the Beanie doc outside this service;
    the analogous principle on the engine side is that the cloud
    surface must remain importable without the engine's optional deps.

    Returns the engine's ``RunResult.as_wire_dict()`` so the caller can
    persist the run as a JSON-shaped blob without leaking dataclass
    types into the persistence layer.
    """
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
    result = await run_scenario(config)
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
        result_dict = await _run_engine_inline(body)
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
        scenario_body = CreateScenarioRequest(
            name=body.name,
            sub_type=body.sub_type,
            n_ticks=body.n_ticks,
            personas=body.personas,
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


__all__ = [
    "GATE_DEFAULT_THRESHOLD",
    "create_backtest",
    "create_scenario_run",
    "get_backtest",
    "get_onboarding_gate",
    "get_scenario_run",
    "list_backtests",
    "list_scenario_runs",
]
