# ee/pocketpaw_ee/cloud/foresight/service.py
# Updated: 2026-05-26 (feat/foresight-v10-insights-llm) — RFC 08 v1.0.
# LLM-driven insights synthesizer + per-workspace synthesizer toggle:
#   - ``get_insights(ctx)`` extended — reads the workspace's
#     ``insights_synthesizer`` config ("pattern" | "llm", default
#     "pattern") and either delegates to the v0.5 pattern synthesizer
#     (unchanged) OR to the new LLM synthesizer
#     (``ee.foresight.insights_llm.synthesize_insights_llm``). LLM
#     failures (timeout, malformed output, rate-limit) fall back to the
#     pattern synthesizer + log a structured warning so the wire
#     response never 5xxs. Wire shape (``InsightsResponse``) unchanged.
#   - New ``get_insights_config(ctx)`` / ``set_insights_config(ctx,
#     body)`` — sibling endpoint to the threshold pair. Reads / writes
#     the per-workspace synthesizer choice. Emits
#     ``foresight.insights_config.updated`` on effective change; no-op
#     write stays quiet.
#   - Lazy engine import for ``insights_llm`` (same pattern as the v0.5
#     synthesizer) — preserves the cloud→engine import-linter contract.
#   - Cost discipline: LLM mode is opt-in only. Default stays "pattern"
#     (deterministic, free). Workspaces with no config doc see the
#     pattern path; only an explicit PUT flips the toggle.
# Updated: 2026-05-26 (feat/foresight-v10-scenario-editor-backend) — RFC
# 08 v1.0 wave 3. ``create_scenario_run`` now honors the optional
# ``custom_scenario_id`` field on the POST body:
#   - When set, the service loads the workspace's saved YAML scenario
#     via :func:`ee.cloud.foresight.scenarios.load_workspace_scenario`,
#     parses it into a :class:`ScenarioConfig` via
#     ``ScenarioConfig.from_yaml``, and uses THAT config for the run
#     instead of the inline ``personas`` + ``sub_type`` + ``n_ticks``
#     fields on the body. Body fields stay on the persisted
#     ``request`` blob for audit but the engine reads the saved YAML.
#   - When both ``custom_scenario_id`` and ``sub_type`` are present,
#     ``custom_scenario_id`` wins (the saved YAML's sub_type drives
#     the engine).
#   - Unknown / cross-tenant ids surface as 422
#     ``foresight.custom_scenario_not_found``.
# Updated: 2026-05-26 (feat/foresight-v10-threshold-override-cloud) — RFC
# 08 v1.0 PR 10. Per-workspace onboarding-gate threshold override:
#   - New ``get_threshold(ctx)`` — returns the resolved
#     :class:`ForesightThresholdResponse` (current / default /
#     is_overridden / updated_at) for the caller's workspace. Reads the
#     ``ForesightWorkspaceConfig`` doc (creates none); collapses absent
#     overrides to the default. Tenancy: 403 when no workspace; 404
#     never (the doc is per-workspace and read is intrinsically scoped).
#   - New ``set_threshold(ctx, body)`` — upserts the workspace's
#     override. ``body.threshold=None`` clears the override (sets the
#     doc field to ``None``); a float ∈ [0.5, 0.95] sets it. Emits
#     ``foresight.threshold.updated`` whenever the effective value
#     changes; a no-op write (same value) does NOT emit.
#   - ``get_onboarding_gate(ctx)`` now reads the workspace's effective
#     threshold via the new ``_resolve_workspace_threshold`` helper
#     instead of the hardcoded ``GATE_DEFAULT_THRESHOLD``. Backward
#     compat: a workspace with no override still sees 0.65 echoed back.
#   - ``create_backtest(ctx, body)`` now resolves the workspace floor
#     before validating the per-run threshold — a workspace that has
#     tightened to 0.80 cannot accept a per-run threshold of 0.70 even
#     though the global default is 0.65. The 422 message reports the
#     effective floor so the operator sees the right number.
#   - The ``_score_backtest`` -> ``ThresholdDecision`` path is
#     unchanged: the caller (``create_backtest``) already passes the
#     resolved threshold through, so the backtest scores against the
#     workspace-effective value automatically. Verified by the new
#     ``test_create_backtest_uses_workspace_override`` test.
#   - The aggregator (``ee.foresight.aggregator``) layer never sees the
#     workspace doc; the cloud service reads + resolves the override at
#     the entry point and threads the resulting float through. The
#     import-linter contract (cloud → engine forbidden) is preserved.
# Updated: 2026-05-26 (feat/foresight-v10-live-snapshot-and-fixes) —
# RFC 08 v1.0 — three changes:
#   1. New ``get_live_snapshot(ctx, run_id)`` — backs
#      ``GET /api/v1/foresight/runs/{id}/live-snapshot``. Reads the
#      run doc + projection list, derives the actual tier mix from
#      ``run.result.tier_distribution``, samples up to 10 projections
#      deterministically, and runs the three anomaly detectors from
#      ``ee.cloud.foresight.live_snapshot``. Cross-tenant 404 via
#      ``_fetch_in_workspace`` (same collapsing rule as the other
#      run-scoped reads).
#   2. ``gate_decision`` Pydantic sub-model integration —
#      ``_compose_gate_decision`` builds a :class:`GateDecision` from
#      the aggregator's :class:`ThresholdDecision.as_wire_dict()` plus
#      a derived ``reason`` and ISO-8601 ``evaluated_at``. The two
#      backtest response mappers (``_to_backtest_response`` /
#      ``_to_backtest_list_item``) now hand back the structured model
#      so the wire shape is strict-validated. Backward-compat —
#      Pydantic's ``model_dump()`` produces the legacy dict shape any
#      caller already keys on.
#   3. ``precedent_seed`` POST plumbing — ``_run_engine_inline`` now
#      threads ``body.precedent_seed`` / ``body.precedent_seeds`` into
#      both the engine ``ScenarioConfig`` AND the cloud-side
#      ``NoOpDecisionGraphRef`` so the persisted
#      ``ForesightProjectedDecision.forward_precedent_decision_id``
#      gets a synthetic, deterministic id whenever the operator opts
#      in. The engine YAML's existing seed support (PR #1235) is
#      preserved — body seeds simply layer onto the same machinery.
# Updated: 2026-05-26 (feat/foresight-v10-prediction-record-persist) —
# RFC 08 v1.0 PR 10 — calibration buffer migration to Mongo:
#   - New ``emit_prediction_record(ctx, payload)`` — engine callback
#     mirrors each per-(anchor × tick) projection into the
#     ``foresight_prediction_records`` collection. Idempotent on
#     (workspace, run_id, tick_id, anchor_id, persona_id).
#   - New ``pair_prediction(ctx, record_id, observed_outcome,
#     pair_delta)`` — flips an existing record to ``paired=True`` and
#     stamps ``observed_at`` / ``observed_outcome`` / ``pair_delta``.
#   - ``_run_engine_inline`` now wires ``on_prediction_record``
#     alongside the existing ``on_projected_decision`` closure so the
#     same engine pass persists BOTH collections (no extra engine work
#     — the prediction-record path is a side-mirror of the projected-
#     decision path).
#   - ``_score_backtest`` now persists paired PredictionRecords for
#     each backtest anchor before scoring, so the §11.5 rolling-accuracy
#     read sees real data instead of the v0.5 ``ForesightBacktest.gate_decision.observed``
#     proxy.
#   - ``get_aggregate_rollup`` REWRITTEN — reads PredictionRecord docs
#     (paired=True, captured_at in window), buckets by day for the
#     rolling series, derives confidence drift from per-record
#     ``confidence`` mean comparison, derives modal distribution from
#     ``prediction.modal_outcome`` counts. Same response shape as PR
#     #1241 (the wire contract is locked).
#   - ``get_insights`` REWRITTEN — reads PredictionRecord docs (no
#     proxy) and composes the synthesizer bundle. Per-persona
#     calibration now comes from real captured records instead of
#     ForesightProjectedDecision.confidence; per-anchor outlier
#     detection has the real data it needs once a workspace runs
#     enough backtests. Same response shape.
#
#   v0.5 → v1.0 behaviour delta: identical wire contract. The
#   aggregate + insights endpoints return the same JSON shape; only
#   the data source flipped from proxy reads to real PredictionRecord
#   reads. Empty-workspace paths still collapse to zeros + empty
#   arrays.
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
from collections.abc import Sequence
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
    ForesightInsightsConfigUpdated,
    ForesightInstinctProposalCreated,
    ForesightOnboardingUnlocked,
    ForesightProjectedDecisionEmitted,
    ForesightRunCompleted,
    ForesightRunCreated,
    ForesightRunFailed,
    ForesightThresholdUpdated,
)
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.foresight.domain import (
    AggregateRollup,
    BacktestRun,
    ConfidenceDrift,
    InsightsConfigView,
    InsightView,
    LiveAnomaly,
    LiveSnapshotView,
    ModalOutcomeEntry,
    OnboardingGateState,
    PredictionRecord,
    ProjectedDecision,
    RollingAccuracyPoint,
    ScenarioCatalogEntry,
    ScenarioRun,
    ThresholdOverrideView,
)
from pocketpaw_ee.cloud.foresight.dto import (
    AggregateRollupResponse,
    Anomaly,
    BacktestRunListItemResponse,
    BacktestRunResponse,
    ConfidenceDriftDto,
    CreateBacktestRequest,
    CreateScenarioRequest,
    ForesightInsightsConfigResponse,
    ForesightInstinctProposalListResponse,
    ForesightInstinctProposalResponse,
    ForesightThresholdResponse,
    GateDecision,
    InsightResponse,
    InsightsResponse,
    LiveSnapshotResponse,
    ModalOutcomeDistributionDto,
    ModalOutcomeEntryDto,
    OnboardingGateResponse,
    ProjectedDecisionListResponse,
    ProjectedDecisionResponse,
    RollingAccuracyPointDto,
    RollingAccuracySeriesDto,
    SampledTrace,
    ScenarioCatalogItem,
    ScenarioCatalogResponse,
    ScenarioRunListItemResponse,
    ScenarioRunResponse,
    SetForesightInsightsConfigRequest,
    SetForesightThresholdRequest,
    TierMixActual,
)
from pocketpaw_ee.cloud.foresight.live_snapshot import (
    DEFAULT_TIER_MIX as _LIVE_DEFAULT_TIER_MIX,
)
from pocketpaw_ee.cloud.foresight.live_snapshot import (
    derive_tier_mix_actual,
    detect_all_anomalies,
    sample_traces,
)
from pocketpaw_ee.cloud.models.foresight_backtest import (
    ForesightBacktest as _ForesightBacktestDoc,
)
from pocketpaw_ee.cloud.models.foresight_prediction_record import (
    ForesightPredictionRecord as _ForesightPredictionRecordDoc,
)
from pocketpaw_ee.cloud.models.foresight_projected_decision import (
    ForesightProjectedDecision as _ForesightProjectedDecisionDoc,
)
from pocketpaw_ee.cloud.models.foresight_run import ForesightRun as _ForesightRunDoc
from pocketpaw_ee.cloud.models.foresight_workspace_config import (
    ForesightWorkspaceConfig as _ForesightWorkspaceConfigDoc,
)

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


async def _build_engine_config_from_body(
    body: CreateScenarioRequest,
    *,
    workspace_id: str | None = None,
) -> Any:
    """Resolve the engine :class:`ScenarioConfig` for one POST body.

    Two paths (wave 3):

      - **inline-personas** (the v0.5 default): the body's ``personas``
        / ``sub_type`` / ``n_ticks`` drive the engine config directly.
        Behaviour identical to PR 7 — added here so both validation
        and run dispatch use one resolver.
      - **custom_scenario_id**: load the workspace's saved YAML scenario
        via :func:`ee.cloud.foresight.scenarios.load_workspace_scenario`,
        write it to a tmp path, and let the engine's
        ``ScenarioConfig.from_yaml`` parse it. The saved YAML wins
        over the request's ``sub_type`` / ``personas`` / ``n_ticks``;
        the body fields are still echoed onto the audit ``request``
        blob but the engine reads the YAML's values.

    Both paths surface engine-side validation errors (unsupported
    sub_type, persona-empty, n_ticks bound) as plain Python exceptions
    the caller catches into 422 ``foresight.invalid_scenario``. The
    workspace-scenario lookup raises ``ValidationError`` directly so a
    missing / cross-tenant id surfaces as
    ``foresight.custom_scenario_not_found``.
    """
    from pocketpaw_ee.foresight.persona import OceanDrift  # noqa: PLC0415
    from pocketpaw_ee.foresight.scenarios.runner import (  # noqa: PLC0415
        PersonaSpec,
        ScenarioConfig,
    )

    if body.custom_scenario_id and workspace_id:
        # Lazy import to keep ``service.py`` from carrying a top-level
        # dependency on the wave-3 scenarios module. The function is
        # async; we await it and let it raise on missing / cross-tenant.
        from pocketpaw_ee.cloud.foresight.scenarios import (  # noqa: PLC0415
            load_workspace_scenario,
        )

        scenario = await load_workspace_scenario(workspace_id, body.custom_scenario_id)

        # Engine ``from_yaml`` expects a path; we round-trip the saved
        # body through a tmp file so the engine's existing parser owns
        # the grammar contract. Keeps the cloud entity free of a
        # parallel parser that could drift from the engine's.
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".yaml",
            delete=False,
            encoding="utf-8",
        ) as fh:
            fh.write(scenario.yaml_body)
            tmp_path = Path(fh.name)
        try:
            config = ScenarioConfig.from_yaml(tmp_path)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

        # Body's optional ``precedent_seed`` overrides the YAML's when
        # supplied; consistent with the inline-personas path where the
        # body's seed wins. ``precedent_seeds`` (per-anchor map) is
        # additive — the YAML's map is the baseline; the body's map
        # layers on top so an operator can override one anchor without
        # rewriting the YAML.
        if body.precedent_seed is not None:
            config.precedent_seed = body.precedent_seed
        if body.precedent_seeds:
            merged = dict(config.precedent_seeds or {})
            merged.update(body.precedent_seeds)
            config.precedent_seeds = merged
        return config

    # Inline-personas path (the v0.5 default).
    personas = [
        PersonaSpec(
            name=p.name,
            role=p.role,
            ocean=OceanDrift(**p.ocean),
        )
        for p in body.personas
    ]
    return ScenarioConfig(
        name=body.name,
        sub_type=body.sub_type,
        n_ticks=body.n_ticks,
        personas=personas,
        precedent_seed=body.precedent_seed,
        precedent_seeds=dict(body.precedent_seeds or {}),
    )


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

    v1.0 wave 3 — when the request carries ``custom_scenario_id``, the
    engine config comes from the saved workspace YAML instead of the
    inline body fields. See :func:`_build_engine_config_from_body`
    for the resolution logic.

    Returns the engine's ``RunResult.as_wire_dict()`` so the caller can
    persist the run as a JSON-shaped blob without leaking dataclass
    types into the persistence layer.
    """
    from pocketpaw_ee.foresight.decision_graph_ref import NoOpDecisionGraphRef  # noqa: PLC0415
    from pocketpaw_ee.foresight.scenarios.runner import run_scenario  # noqa: PLC0415

    config = await _build_engine_config_from_body(body, workspace_id=workspace_id)

    # v1.0 PR (§14.4 body plumbing) — pass the request's optional
    # ``precedent_seed`` / ``precedent_seeds`` into the engine config so
    # the runner's own NoOp ref construction matches what the cloud
    # closure builds below. The two refs (engine-side + cloud-side) must
    # agree on seeds so the per-record id the runner stamps on
    # ``RunResult.projected_decisions`` matches the per-record id the
    # cloud closure persists on ``ForesightProjectedDecision.forward_precedent_decision_id``.
    #
    # The config's own seeds are already the merged result (custom
    # scenarios start from the YAML + layer the body on top); we mirror
    # them on the cloud-side ref below so the lookups stay deterministic.
    seed_value = config.precedent_seed or ""
    per_anchor_seeds = dict(config.precedent_seeds or {})

    # v14 (§14.4) — build a DecisionGraphRef mirroring the engine's
    # default so the cloud closure can stamp the same
    # ``forward_precedent_decision_id`` value the runner computes for
    # ``RunResult.projected_decisions``. v1.0 lifts the body seeds onto
    # this ref so a POST that supplies ``precedent_seed`` produces a
    # synthetic, deterministic precedent id for every persisted
    # projection. When no seed is supplied the ref behaves exactly as
    # before — :class:`NoOpDecisionGraphRef` returns ``None`` for every
    # lookup and the wire shape collapses to the v0.5 "always None"
    # default.
    decision_graph_ref = NoOpDecisionGraphRef(
        seed=seed_value,
        per_anchor_seeds=per_anchor_seeds,
    )

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
        # ``scenario_name`` is the precedent-lookup key; the engine
        # writes its ``RunResult.projected_decisions`` records using the
        # ScenarioConfig's name, so we mirror that here. For inline
        # bodies the two match (``config.name == body.name``); for
        # custom_scenario_id runs the saved YAML's name wins so the
        # cloud-side ref agrees with the engine on the per-record id.
        scenario_name = config.name

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

    # PR 10 — PredictionRecord mirror callback. Persists each
    # per-(anchor × tick) projection into the
    # ``foresight_prediction_records`` collection. The closure stays
    # here (not in the runner) so the engine never statically imports
    # the cloud — the import-linter contract holds. The callback is
    # only wired when the cloud caller has already resolved a
    # workspace; CLI smoke runs pass no workspace_id and the prediction
    # buffer mirror stays a no-op (the in-engine
    # ``RunResult.projected_decisions`` field still carries the same
    # records for direct consumption).
    prediction_callback = None
    if workspace_id and run_id:

        async def _on_prediction_record(payload: dict[str, Any]) -> None:
            await emit_prediction_record(
                workspace_id=workspace_id,
                payload=payload,
            )

        prediction_callback = _on_prediction_record

    result = await run_scenario(
        config,
        on_projected_decision=callback,
        on_prediction_record=prediction_callback,
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
    #
    # v1.0 wave 3: ``_build_engine_config_from_body`` honors
    # ``custom_scenario_id`` when set — the saved YAML's grammar is the
    # validation source of truth in that case; otherwise the inline
    # personas / sub_type / n_ticks fields validate as before.
    try:
        _ = await _build_engine_config_from_body(body, workspace_id=workspace_id)
    except ValidationError:
        # ``load_workspace_scenario`` raises this with the
        # ``foresight.custom_scenario_not_found`` code — re-raise
        # untouched so the caller's 422 carries the right code.
        raise
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
    ctx: RequestContext, *, limit: int = 50, offset: int = 0
) -> list[ScenarioRunListItemResponse]:
    """List runs in the caller's workspace, most recent first.

    ``limit`` caps the response at 50 by default; the frontend's
    Scenarios panel paginates beyond that. The lighter
    :class:`ScenarioRunListItemResponse` shape drops the inline
    ``result`` blob so a workspace with a hundred runs still serves
    the list endpoint in tens of kilobytes rather than megabytes.

    ``offset`` is the server-side cursor for pagination. Defaults to
    ``0`` so existing single-page callers stay correct; negative values
    raise ``foresight.invalid_offset`` (same pattern as
    :func:`list_projected_decisions`). Mongo's ``.skip(offset)`` runs
    inside the same sort + tenant filter so the ordering stays stable
    across pages.
    """
    workspace_id = _require_workspace(ctx)
    if limit < 1:
        raise ValidationError("foresight.invalid_limit", "limit must be >= 1")
    if limit > 200:
        # Hard cap so a misconfigured caller can't drag the entire
        # collection into memory; the frontend never asks for more.
        limit = 200
    if offset < 0:
        raise ValidationError("foresight.invalid_offset", "offset must be >= 0")

    # Tenant filter on every read per cloud rule #7. Sort newest first
    # so the Scenarios panel renders most-recent-on-top without a
    # client-side reorder pass. ``_id`` tiebreaker keeps the ordering
    # stable when ``createdAt`` collides at sub-millisecond resolution
    # (a hot create loop will produce ties under in-memory Mongo and
    # under sub-millisecond Mongo clocks in production).
    docs = (
        await _ForesightRunDoc.find({"workspace": workspace_id})
        .sort([("createdAt", -1), ("_id", -1)])  # type: ignore[list-item]
        .skip(offset)
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


def _compose_gate_decision(
    raw: dict[str, Any] | None,
    *,
    fallback_threshold: float,
    fallback_evaluated_at: datetime | None,
) -> GateDecision | None:
    """Compose a :class:`GateDecision` from the persisted gate dict.

    The persisted dict is the
    :meth:`ee.foresight.aggregator.ThresholdDecision.as_wire_dict()`
    payload (``passed`` / ``observed`` / ``threshold`` / ``margin`` /
    ``n_pairs``). v1.0 promotes that loose dict to a structured
    Pydantic model so the wire surface is strict-validated; this
    helper builds the model with two derived fields:

    - ``reason`` — short label derived from ``passed`` + ``n_pairs``.
      Vocabulary: ``no_pairs`` (n_pairs == 0), ``threshold_met``
      (passed=True), ``threshold_unmet`` (passed=False, n_pairs >= 1).
    - ``evaluated_at`` — ISO-8601 UTC timestamp. Reads
      ``raw["evaluated_at"]`` when the write path populated it (v1.0+);
      falls back to the backtest doc's ``updatedAt`` (the moment the
      doc flipped to status="complete"), and finally to "now" so the
      field is always populated.
    - ``modal_accuracy`` — alias for ``observed`` so the UI lead's
      TypeScript shape stays readable. Same float on both fields so
      they never diverge.

    Returns ``None`` when the input is ``None`` (backtest still
    queued/running/failed). Malformed input (missing required keys,
    out-of-range floats) raises a ``ValidationError`` via Pydantic
    — caught at the service write-site so a bad gate payload never
    silently degrades the wire surface.
    """
    if raw is None:
        return None

    observed_value = raw.get("observed")
    if observed_value is None:
        observed_value = raw.get("modal_accuracy", 0.0)
    threshold_value = raw.get("threshold", fallback_threshold)
    n_pairs_value = int(raw.get("n_pairs", 0) or 0)
    passed_value = bool(raw.get("passed", False))

    if n_pairs_value == 0:
        reason = "no_pairs"
    elif passed_value:
        reason = "threshold_met"
    else:
        reason = "threshold_unmet"
    # Free-form override — a v1.0+ write path may pin a custom reason
    # (e.g. a future "thin_sample" surface); honour it when present.
    raw_reason = raw.get("reason")
    if isinstance(raw_reason, str) and raw_reason:
        reason = raw_reason

    evaluated_at_raw = raw.get("evaluated_at")
    evaluated_at_iso: str | None = None
    if isinstance(evaluated_at_raw, str) and evaluated_at_raw:
        evaluated_at_iso = evaluated_at_raw
    elif isinstance(evaluated_at_raw, datetime):
        evaluated_at_iso = iso_utc(evaluated_at_raw)
    if not evaluated_at_iso:
        evaluated_at_iso = iso_utc(fallback_evaluated_at) or iso_utc(datetime.now(UTC_TZ)) or ""

    observed_float = float(observed_value or 0.0)
    # Clamp the floats into [0, 1] — the aggregator already produces
    # values in that range; the clamp guards against malformed
    # historical writes (a stray "1.0001" would otherwise 422 the
    # response which would be worse than rounding).
    observed_clamped = max(0.0, min(1.0, observed_float))
    threshold_clamped = max(0.0, min(1.0, float(threshold_value or 0.0)))
    margin_value = raw.get("margin")
    if isinstance(margin_value, (int, float)):
        margin_float = float(margin_value)
    else:
        margin_float = observed_clamped - threshold_clamped

    return GateDecision(
        passed=passed_value,
        threshold=threshold_clamped,
        observed=observed_clamped,
        modal_accuracy=observed_clamped,
        margin=round(margin_float, 4),
        n_pairs=n_pairs_value,
        reason=reason,
        evaluated_at=evaluated_at_iso,
    )


def _to_backtest_response(run: BacktestRun) -> BacktestRunResponse:
    gate_decision = _compose_gate_decision(
        run.gate_decision,
        fallback_threshold=run.threshold,
        fallback_evaluated_at=run.updated_at or run.created_at,
    )
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
        gate_decision=gate_decision,
        error=run.error,
    )


def _to_backtest_list_item(run: BacktestRun) -> BacktestRunListItemResponse:
    gate_decision = _compose_gate_decision(
        run.gate_decision,
        fallback_threshold=run.threshold,
        fallback_evaluated_at=run.updated_at or run.created_at,
    )
    return BacktestRunListItemResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        scenario_name=run.scenario_name,
        status=run.status,
        created_at=iso_utc(run.created_at) or "",
        updated_at=iso_utc(run.updated_at),
        threshold=run.threshold,
        gate_decision=gate_decision,
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


def _resolve_threshold(requested: float | None, *, floor: float = GATE_DEFAULT_THRESHOLD) -> float:
    """Pick the effective threshold for one backtest run.

    v1.0 (this PR): the floor is now the workspace's effective threshold
    (the workspace admin's override when set, otherwise the global
    :data:`GATE_DEFAULT_THRESHOLD`). A workspace that has tightened to
    0.80 cannot accept a per-run threshold of 0.70 even though the
    global default is 0.65 — the operator must tighten above the
    workspace floor, never relax below it. The 422 message reports the
    floor so the operator sees the right number.

    v0.1 took a single argument and always compared against
    ``GATE_DEFAULT_THRESHOLD``. The new ``floor`` kwarg defaults to that
    constant so any unmigrated caller (none in tree) behaves identically.
    """
    if requested is None:
        return floor
    if requested < floor:
        raise ValidationError(
            "foresight.threshold_below_default",
            f"per-run threshold {requested} cannot relax below the workspace "
            f"floor {floor}; tighten above the floor only",
        )
    return requested


# ---------------------------------------------------------------------------
# Per-workspace threshold override (RFC 08 v1.0 PR 10).
#
# Workspaces can persist a per-workspace override above the global
# default (0.5–0.95 inclusive, enforced at the DTO layer). The override
# is read by ``get_onboarding_gate`` and by ``create_backtest`` so all
# gate-scoping reads see the same effective value.
#
# Two reads:
#   - ``_resolve_workspace_threshold`` returns just the float (used in
#     hot paths that don't need the full view, e.g. the gate read).
#   - ``_load_threshold_view`` returns the full
#     :class:`ThresholdOverrideView` (used by the GET / PUT response
#     mappers).
# ---------------------------------------------------------------------------


async def _load_threshold_view(workspace_id: str) -> ThresholdOverrideView:
    """Compose the workspace's threshold-override view.

    A workspace with no config doc, or with a doc whose
    ``threshold_override`` is ``None``, collapses to a "no override"
    view — ``current_threshold`` equals the default, ``is_overridden``
    is ``False``, ``updated_at`` is ``None``. The read never raises (an
    absent doc is the normal case for a fresh workspace).
    """
    doc = await _ForesightWorkspaceConfigDoc.find_one(
        {"workspace": workspace_id},
    )
    if doc is None or doc.threshold_override is None:
        return ThresholdOverrideView(
            workspace_id=workspace_id,
            current_threshold=GATE_DEFAULT_THRESHOLD,
            default_threshold=GATE_DEFAULT_THRESHOLD,
            is_overridden=False,
            updated_at=None,
        )
    return ThresholdOverrideView(
        workspace_id=workspace_id,
        current_threshold=float(doc.threshold_override),
        default_threshold=GATE_DEFAULT_THRESHOLD,
        is_overridden=True,
        updated_at=doc.updatedAt,
    )


async def _resolve_workspace_threshold(workspace_id: str) -> float:
    """Return the workspace's effective onboarding-gate threshold.

    Light wrapper around :func:`_load_threshold_view` that pulls just the
    float — used by hot paths (``get_onboarding_gate``, ``create_backtest``)
    that don't need the full view shape.
    """
    view = await _load_threshold_view(workspace_id)
    return view.current_threshold


def _to_threshold_response(view: ThresholdOverrideView) -> ForesightThresholdResponse:
    """Map the domain view to the wire DTO."""
    return ForesightThresholdResponse(
        workspace_id=view.workspace_id,
        current_threshold=view.current_threshold,
        default_threshold=view.default_threshold,
        is_overridden=view.is_overridden,
        updated_at=iso_utc(view.updated_at),
    )


async def get_threshold(ctx: RequestContext) -> ForesightThresholdResponse:
    """Return the workspace's resolved onboarding-gate threshold view.

    GET /api/v1/foresight/workspace/threshold backing.

    Tenancy:
      - 403 ``foresight.no_workspace`` when the caller has no active
        workspace.
      - No cross-tenant reads possible — the view is intrinsically
        per-workspace; an absent override collapses to the default view
        (never 404).
    """
    workspace_id = _require_workspace(ctx)
    # no-event: read-only path; emit only on writes (cloud rule #9).
    view = await _load_threshold_view(workspace_id)
    return _to_threshold_response(view)


async def set_threshold(
    ctx: RequestContext,
    body: SetForesightThresholdRequest,
) -> ForesightThresholdResponse:
    """Upsert the workspace's onboarding-gate threshold override.

    PUT /api/v1/foresight/workspace/threshold backing.

    ``body.threshold=None`` resets the workspace to the global default
    (writes ``threshold_override=None`` on the doc — the doc itself
    survives, since a "previously overridden, now reset" workspace has
    an audit-relevant updatedAt). A float ∈ [0.5, 0.95] sets the
    override; DTO-level bounds enforce the range so a 422 fires before
    this function runs.

    Emit semantics:
      - When the effective value changes (override → different override,
        override → reset, no-override → set), fire
        ``foresight.threshold.updated`` with ``data={
            workspace_id, threshold (new effective), is_overridden,
            previous_threshold, previous_is_overridden,
        }`` so listeners (UI panels, audit log) can react without a
        round trip.
      - A no-op write (same value as the current effective threshold)
        does NOT emit — keeps the UI's optimistic-local-state path from
        rebroadcasting redundant updates.
    """
    body = SetForesightThresholdRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)

    previous_view = await _load_threshold_view(workspace_id)
    new_override = body.threshold  # already validated to None or ∈ [0.5, 0.95]

    # Upsert pattern — Beanie has no native upsert helper that returns
    # the resulting doc, so split into find_one + insert / update. The
    # workspace field is unique-indexed so a concurrent insert race
    # would surface as a DuplicateKeyError; for v1.0's admin-only write
    # path that race is unlikely enough to treat as a 500 (no retry
    # loop). v1.1 can swap to Mongo's $setOnInsert if the race shows up.
    doc = await _ForesightWorkspaceConfigDoc.find_one(
        {"workspace": workspace_id},
    )
    if doc is None:
        doc = _ForesightWorkspaceConfigDoc(
            workspace=workspace_id,
            threshold_override=new_override,
        )
        await doc.insert()
    else:
        doc.threshold_override = new_override
        await doc.save()

    new_view = await _load_threshold_view(workspace_id)
    response = _to_threshold_response(new_view)

    # Emit only when the effective value changed. A no-op write
    # (e.g. PUT {"threshold": 0.7} when the override is already 0.7)
    # stays quiet so the UI's optimistic state doesn't get echoed.
    changed = (
        previous_view.current_threshold != new_view.current_threshold
        or previous_view.is_overridden != new_view.is_overridden
    )
    if changed:
        await emit(
            ForesightThresholdUpdated(
                data={
                    "workspace_id": workspace_id,
                    "threshold": new_view.current_threshold,
                    "is_overridden": new_view.is_overridden,
                    "previous_threshold": previous_view.current_threshold,
                    "previous_is_overridden": previous_view.is_overridden,
                }
            )
        )
    # else: no-event: idempotent write (cloud rule #9 allows this with
    # the inline comment); the GET still returns the resolved view so
    # the client's PUT round-trip stays useful.
    return response


async def _score_backtest(
    body: CreateBacktestRequest,
    *,
    engine_result: dict[str, Any],
    threshold: float,
    workspace_id: str | None = None,
    backtest_run_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pair the engine's projected outcomes against the operator-supplied
    actual outcomes, aggregate, and score against the threshold.

    Returns ``(summary_wire_dict, gate_decision_wire_dict)``.

    v0.1 simulated the §10 pair-against-reality loop in-process — every
    anchor in ``body.anchors`` produced one ``CalibrationPair`` whose
    projected outcome came from the engine's per-tick projection (when
    available) or a placeholder ``{}``. PR 10 (v1.0) ALSO persists one
    paired :class:`ForesightPredictionRecord` per anchor when
    ``workspace_id`` + ``backtest_run_id`` are supplied — this is how
    the §11.5 rolling-accuracy read sees historical backtest data
    without falling back to the v0.5 gate-decision-observed proxy.

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

    # PR 10 — persist paired PredictionRecords for the rolling-accuracy
    # read. Each anchor produces ONE Mongo row stamped with the
    # pair_delta the in-memory aggregator already computed. Done after
    # scoring so a scoring exception never half-persists the batch.
    # Caller passes ``workspace_id`` + ``backtest_run_id`` when the
    # backtest is real (i.e. ``create_backtest`` is calling us); test
    # callers can leave them None to keep the legacy in-memory-only
    # path.
    if workspace_id and backtest_run_id:
        for record, pair in zip(records, pairs, strict=True):
            # Compute a per-pair match flag and surface it on the
            # stored ``pair_delta`` payload so the rolling-accuracy
            # read can count matches without re-running the aggregator.
            # Pair-level match = no metric fell outside the numeric
            # tolerance band (delegates to the aggregator's own
            # ``_metric_matches`` semantics by counting per-metric
            # match flags inside ``pair.delta``).
            payload: dict[str, Any] = {
                "anchor_id": record.anchor_object_id,
                "persona_id": "",
                "tick_id": 0,
                "decision_text": str(
                    projected_outcome_default.get("modal_outcome", "")
                    or projected_outcome_default.get("outcome", "")
                ),
                "confidence": float(record.projection_confidence),
                "sub_type": str(body.sub_type),
                "scenario_id": str(body.name),
                "run_id": backtest_run_id,
                "prediction": dict(projected_outcome_default),
            }
            persisted = await emit_prediction_record(
                workspace_id=workspace_id,
                payload=payload,
            )
            # Stamp observed_outcome + pair_delta so the rolling-accuracy
            # window read can filter on ``paired=True`` and reproduce
            # the per-pair match flag without a second engine call.
            await pair_prediction(
                workspace_id=workspace_id,
                record_id=persisted.id,
                observed_outcome=dict(pair.actual_outcome),
                pair_delta=dict(pair.delta),
            )

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
    workspace_floor = await _resolve_workspace_threshold(workspace_id)
    threshold = _resolve_threshold(body.threshold, floor=workspace_floor)

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
            # PR 10 — pass tenancy + run id so paired PredictionRecord
            # rows persist alongside the in-memory aggregator pass.
            # Drives the §11.5 rolling-accuracy read off the new
            # ``foresight_prediction_records`` collection instead of
            # the v0.5 ``ForesightBacktest.gate_decision.observed``
            # proxy.
            workspace_id=workspace_id,
            backtest_run_id=str(doc.id),
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
    ctx: RequestContext, *, limit: int = 50, offset: int = 0
) -> list[BacktestRunListItemResponse]:
    """List backtests in the caller's workspace, most recent first.

    Same shape conventions as ``list_scenario_runs``: lighter list-item
    DTO that drops the inline result blob but keeps ``gate_decision`` so
    the Aggregate panel can render the unlock label per row without
    needing the detail endpoint.

    ``offset`` is the server-side cursor for pagination — mirrors the
    cursor added to :func:`list_scenario_runs` so the agent_context
    wrapper paginates at Mongo's ``.skip()`` step rather than
    over-fetching and slicing client-side. Defaults to ``0`` so every
    existing caller (router, tests) stays correct; negative values
    raise ``foresight.invalid_offset``.
    """
    workspace_id = _require_workspace(ctx)
    if limit < 1:
        raise ValidationError("foresight.invalid_limit", "limit must be >= 1")
    if limit > 200:
        limit = 200
    if offset < 0:
        raise ValidationError("foresight.invalid_offset", "offset must be >= 0")

    docs = (
        await _ForesightBacktestDoc.find({"workspace": workspace_id})
        .sort([("createdAt", -1), ("_id", -1)])  # type: ignore[list-item]
        .skip(offset)
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

    The threshold echoed back is the workspace's effective floor —
    the per-workspace override when set, else the global
    :data:`GATE_DEFAULT_THRESHOLD`. v1.0 reads the
    :class:`pocketpaw_ee.cloud.models.foresight_workspace_config.ForesightWorkspaceConfig`
    doc via :func:`_resolve_workspace_threshold` (which handles the
    absent-doc and null-override cases).
    """
    workspace_id = _require_workspace(ctx)
    threshold = await _resolve_workspace_threshold(workspace_id)

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
# PredictionRecord persistence (RFC 08 §9.1 CAPTURE / §9.2 OBSERVE + PR 10)
#
# The cloud-side mirror of the engine's :class:`PredictionBuffer`. The
# engine never imports cloud — the callbacks below are injected via
# closure into ``run_scenario`` so the import direction stays
# cloud → engine.
#
# Two writes happen here:
#
#   1. ``emit_prediction_record`` — one INSERT per (anchor × tick) bucket
#      as the engine ticks the run forward. Idempotent on
#      (workspace, run_id, tick_id, anchor_id, persona_id) so a re-emit
#      of the same bucket replays without creating duplicate rows.
#   2. ``pair_prediction`` — UPDATE flipping ``paired=True`` and
#      stamping ``observed_at`` / ``observed_outcome`` / ``pair_delta``
#      once reality lands. Backtests pair every anchor at scoring time;
#      forward sims pair via the v1.1+ outcome-listener stream.
# ---------------------------------------------------------------------------


def _to_prediction_record_domain(
    doc: _ForesightPredictionRecordDoc,
) -> PredictionRecord:
    return PredictionRecord(
        id=str(doc.id),
        workspace_id=doc.workspace,
        anchor_id=doc.anchor_id,
        persona_id=doc.persona_id,
        scenario_id=doc.scenario_id,
        run_id=doc.run_id,
        tick_id=doc.tick_id,
        prediction=dict(doc.prediction or {}),
        confidence=doc.confidence,
        captured_at=doc.captured_at,
        observed_at=doc.observed_at,
        observed_outcome=dict(doc.observed_outcome) if doc.observed_outcome else None,
        paired=doc.paired,
        pair_delta=dict(doc.pair_delta) if doc.pair_delta else None,
    )


async def emit_prediction_record(
    *,
    workspace_id: str,
    payload: dict[str, Any],
) -> PredictionRecord:
    """Persist one PredictionRecord row, idempotent on the bucket key.

    Called by the engine's per-tick callback (wired in
    :func:`_run_engine_inline`). The bucket key is
    ``(workspace, run_id, tick_id, anchor_id, persona_id)`` — the same
    quintuple the engine's :class:`PredictionBuffer` uses to dedupe
    in-memory. A re-emit of the same bucket (re-run of a fixed-seed
    scenario, retry after a transient error) returns the existing row
    instead of inserting a duplicate.

    Tenancy:
      - ``workspace_id`` is required (the closure in
        :func:`_run_engine_inline` only wires when the cloud caller
        has already resolved a workspace), so this function asserts
        rather than validating.

    No event is emitted — the engine's projected-decision fan-out
    already fires :class:`ForesightProjectedDecisionEmitted` for the
    same (anchor × tick) bucket, and downstream subscribers reading
    that event have everything they need. Adding a parallel
    "prediction_record.captured" event would double-fire without new
    information.
    """
    if not workspace_id:
        raise Forbidden(
            "foresight.no_workspace",
            "workspace required to emit a prediction record",
        )

    captured_at = datetime.now(UTC_TZ)
    run_id = str(payload.get("run_id", ""))
    tick_id = int(payload.get("tick_id", 0))
    anchor_id = str(payload.get("anchor_id", ""))
    persona_id = str(payload.get("persona_id", ""))

    # Idempotence guard — read-before-write on the bucket quintuple.
    # The query is bounded by the (workspace, anchor_id, captured_at)
    # index; for a given run + tick + anchor + persona the result set
    # is at most one row.
    existing = await _ForesightPredictionRecordDoc.find_one(
        {
            "workspace": workspace_id,
            "run_id": run_id,
            "tick_id": tick_id,
            "anchor_id": anchor_id,
            "persona_id": persona_id,
        }
    )
    if existing is not None:
        return _to_prediction_record_domain(existing)

    doc = _ForesightPredictionRecordDoc(
        workspace=workspace_id,
        anchor_id=anchor_id,
        persona_id=persona_id,
        scenario_id=str(payload.get("scenario_id", "")),
        run_id=run_id,
        tick_id=tick_id,
        prediction=dict(payload.get("prediction") or {}),
        confidence=float(payload.get("confidence", 0.0)),
        captured_at=captured_at,
    )
    await doc.insert()
    return _to_prediction_record_domain(doc)


async def pair_prediction(
    *,
    workspace_id: str,
    record_id: str,
    observed_outcome: dict[str, Any],
    pair_delta: dict[str, Any] | None = None,
) -> PredictionRecord:
    """Mark a captured PredictionRecord as paired with reality.

    Flips ``paired=True`` and stamps ``observed_at`` /
    ``observed_outcome`` / ``pair_delta``. The ``pair_delta`` is the
    diff dict :func:`ee.foresight.calibration._compute_delta` produces;
    callers (backtests, future outcome listeners) compute it via the
    engine helper and hand the result in.

    Tenancy: scoped by ``workspace_id`` so a cross-tenant id collapses
    to NotFound — keeps existence non-leakable across tenants.

    No event emitted — same rationale as :func:`emit_prediction_record`
    (the projection / backtest path already fires the dominant event).
    """
    if not workspace_id:
        raise Forbidden(
            "foresight.no_workspace",
            "workspace required to pair a prediction record",
        )

    try:
        oid = PydanticObjectId(record_id)
    except Exception:
        raise NotFound("foresight_prediction_record", record_id) from None

    doc = await _ForesightPredictionRecordDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("foresight_prediction_record", record_id)

    doc.observed_at = datetime.now(UTC_TZ)
    doc.observed_outcome = dict(observed_outcome)
    doc.paired = True
    if pair_delta is not None:
        doc.pair_delta = dict(pair_delta)
    await doc.save()
    return _to_prediction_record_domain(doc)


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
# PR 10 (v1.0) reads the workspace's persisted ``ForesightPredictionRecord``
# docs over the window:
#
#   - Rolling accuracy: paired records bucketed by day; per-bucket
#     accuracy is the share of records whose ``pair_delta`` shows every
#     metric inside the 10% tolerance band.
#   - Confidence drift: earliest vs latest record-mean ``confidence``
#     within the window; ``rising`` / ``falling`` / ``flat`` per the
#     §11.5 vocabulary with a 5% flat threshold.
#   - Modal outcome distribution: ``prediction.modal_outcome`` tally
#     normalised by total record count.
#
# v0.5 used proxies (``ForesightBacktest.gate_decision.observed`` for
# rolling, ``ForesightProjectedDecision`` for modal dist). v1.0 reads
# real PredictionRecord rows — wire shape locked, only data source
# flipped.
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


def _pair_is_match(pair_delta: dict[str, Any] | None, tolerance: float = 0.10) -> bool:
    """Decide whether one paired record's ``pair_delta`` counts as a
    match (every metric inside the numeric tolerance band, every
    string metric flagged ``match=True``, no ``missing_in`` markers).

    Mirrors the semantics of
    :func:`ee.foresight.calibration._metric_matches` but stays local
    to the cloud module so we don't have to import the engine for a
    1-line check. Empty / None pair_delta means we never observed
    reality matching the projection — treat as non-match (False) so
    such records don't inflate accuracy.
    """
    if not pair_delta:
        return False
    for delta_val in pair_delta.values():
        if isinstance(delta_val, dict):
            if "missing_in" in delta_val:
                return False
            if not bool(delta_val.get("match", False)):
                return False
        elif isinstance(delta_val, (int, float)):
            if abs(delta_val) > tolerance:
                return False
        else:
            return False
    return True


def _compose_rolling_accuracy_from_records(
    paired_records: list[Any],
    *,
    now: datetime,
) -> tuple[RollingAccuracyPoint, ...]:
    """Bucket paired PredictionRecord docs by day; emit one point per
    bucket where the accuracy is the share of records flagged
    ``match`` (every metric inside tolerance).

    Empty buckets are omitted — the UI fills gaps from the bucket
    end-timestamp grid.
    """
    buckets: dict[datetime, list[bool]] = {}
    for doc in paired_records:
        ts = getattr(doc, "captured_at", None) or now
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC_TZ)
        bucket_ts = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        is_match = _pair_is_match(getattr(doc, "pair_delta", None))
        buckets.setdefault(bucket_ts, []).append(is_match)

    points: list[RollingAccuracyPoint] = []
    for ts, flags in sorted(buckets.items()):
        if not flags:
            continue
        accuracy = sum(1 for f in flags if f) / len(flags)
        points.append(
            RollingAccuracyPoint(
                ts=ts,
                accuracy=round(max(0.0, min(1.0, accuracy)), 4),
                sample_count=len(flags),
            )
        )
    return tuple(points)


def _compose_confidence_drift_from_records(
    all_records: list[Any],
    *,
    flat_threshold: float = 0.05,
) -> ConfidenceDrift:
    """Compute the §11.5 confidence-drift summary from PredictionRecord
    docs.

    Compares the per-record ``confidence`` mean for the oldest day
    bucket vs the newest day bucket in the window. Bucketing damps
    single-outlier noise — a single low-confidence record on day 1
    can't fake a "falling" trend by itself.

    Vocabulary (RFC §11.5):
      - ``rising``   — confidence improved (delta > flat_threshold)
      - ``falling``  — confidence degraded (delta < -flat_threshold)
      - ``flat``     — within the flat band (or no data)
    """
    if not all_records:
        return ConfidenceDrift(trend="flat", magnitude=0.0)
    by_day: dict[datetime, list[float]] = {}
    for doc in all_records:
        conf = getattr(doc, "confidence", None)
        if not isinstance(conf, (int, float)):
            continue
        ts = getattr(doc, "captured_at", None)
        if not isinstance(ts, datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC_TZ)
        bucket_ts = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        by_day.setdefault(bucket_ts, []).append(float(conf))
    if len(by_day) < 2:
        return ConfidenceDrift(trend="flat", magnitude=0.0)
    days_sorted = sorted(by_day.items())
    earliest_mean = sum(days_sorted[0][1]) / len(days_sorted[0][1])
    latest_mean = sum(days_sorted[-1][1]) / len(days_sorted[-1][1])
    delta = latest_mean - earliest_mean
    magnitude = abs(delta)
    trend: Literal["rising", "falling", "flat"]
    if magnitude < flat_threshold:
        trend = "flat"
    elif delta > 0:
        trend = "rising"
    else:
        trend = "falling"
    return ConfidenceDrift(trend=trend, magnitude=round(magnitude, 4))


def _compose_modal_outcome_distribution_from_records(
    records: list[Any],
) -> tuple[ModalOutcomeEntry, ...]:
    """Tally ``prediction.modal_outcome`` verbs across PredictionRecord
    docs and normalise into a share map. Records without a modal
    outcome key collapse to no contribution.
    """
    counts: dict[str, int] = {}
    for doc in records:
        prediction = getattr(doc, "prediction", None) or {}
        if not isinstance(prediction, dict):
            continue
        verb = str(prediction.get("modal_outcome", "")).strip().lower()
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


async def _fetch_prediction_records(
    workspace_id: str,
    *,
    window_days: int,
    now: datetime,
    paired_only: bool = False,
) -> list[Any]:
    """Read PredictionRecord docs in the window. Tenant filter on every
    read per cloud rule #7.

    ``paired_only=True`` returns only records where the observation
    landed — the §11.5 rolling-accuracy series uses this filter so
    unpaired forward-sim projections don't inflate the denominator.
    """
    window_start = now - timedelta(days=window_days)
    query: dict[str, Any] = {
        "workspace": workspace_id,
        "captured_at": {"$gte": window_start},
    }
    if paired_only:
        query["paired"] = True
    return await (
        _ForesightPredictionRecordDoc.find(query)
        .sort([("captured_at", 1), ("_id", 1)])  # type: ignore[list-item]
        .to_list()
    )


async def _fetch_latest_backtest(workspace_id: str) -> Any | None:
    """Read the newest completed backtest in the workspace (for the
    §11.6 ``threshold_unmet`` insight rule). Returns ``None`` when no
    completed backtest exists — the rule then silently skips.
    """
    docs = await (
        _ForesightBacktestDoc.find({"workspace": workspace_id, "status": "complete"})
        .sort([("createdAt", -1), ("_id", -1)])  # type: ignore[list-item]
        .limit(1)
        .to_list()
    )
    return docs[0] if docs else None


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

    PR 10 (v1.0): reads :class:`ForesightPredictionRecord` docs instead
    of the v0.5 proxies. ``rolling_accuracy`` filters to paired
    records (``paired=True``); ``confidence_drift`` reads per-record
    ``confidence`` across all records (paired or not) over the
    window; ``modal_outcome_distribution`` tallies the
    ``prediction.modal_outcome`` vocabulary. Wire shape unchanged.
    """
    workspace_id = _require_workspace(ctx)
    effective_window = _resolve_window_days(window_days)
    now = datetime.now(UTC_TZ)

    # Two reads — paired-only for the rolling-accuracy series, full
    # set for the confidence-drift + modal-distribution rollups.
    paired_records = await _fetch_prediction_records(
        workspace_id,
        window_days=effective_window,
        now=now,
        paired_only=True,
    )
    all_records = await _fetch_prediction_records(
        workspace_id,
        window_days=effective_window,
        now=now,
        paired_only=False,
    )

    rolling = _compose_rolling_accuracy_from_records(paired_records, now=now)
    drift = _compose_confidence_drift_from_records(all_records)
    distribution = _compose_modal_outcome_distribution_from_records(all_records)

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
    prediction_records: list[Any],
    *,
    floor_threshold: float,
) -> list[Any]:
    """Build the per-persona calibration view from PredictionRecord docs.

    PR 10 (v1.0): reads real :class:`ForesightPredictionRecord` rows
    instead of the v0.5 ``ForesightProjectedDecision.confidence``
    proxy. The persona's mean per-record confidence becomes the
    calibration figure; the persona_outlier rule downstream gates on
    the floor_threshold so low-confidence personas surface as
    insights.

    The synthesizer's ``PerPersonaCalibration`` dataclass lives in
    the engine namespace; we import it lazily inside the function so
    the cloud module stays clean of the engine layer at module top.
    """
    from pocketpaw_ee.foresight.insights import PerPersonaCalibration

    per_persona: dict[str, list[float]] = {}
    for doc in prediction_records:
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
        # Drop personas with samples below 2 to avoid noise from a
        # single low projection.
        if len(confidences) < 2:
            continue
        mean_conf = sum(confidences) / len(confidences)
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
    latest_backtest: Any | None,
) -> Any | None:
    """Build the synthesizer's ``LatestBacktestGate`` from a single
    backtest doc. Returns ``None`` when the doc is missing or its
    gate payload is malformed — the threshold_unmet rule then silently
    skips.
    """
    from pocketpaw_ee.foresight.insights import LatestBacktestGate

    if latest_backtest is None:
        return None
    doc = latest_backtest
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


async def _fetch_recent_backtest_docs(
    workspace_id: str,
    *,
    window_days: int,
    now: datetime,
) -> list[Any]:
    """Read completed backtest docs in the window — kept around for the
    ``tier_distribution_deltas`` synthesizer field (each backtest's
    ``result.tier_distribution`` reports the per-tier persona count
    the run actually used). The rolling-accuracy series no longer
    derives from backtests; it reads paired PredictionRecord rows
    directly per PR 10.
    """
    window_start = now - timedelta(days=window_days)
    return await (
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

    Composes the synthesizer input bundle from PredictionRecord docs
    (the same source the aggregate endpoint reads), then delegates
    rule evaluation to either:

      - the v0.5 deterministic five-rule synthesizer
        (:func:`ee.foresight.insights.synthesize_insights`), when the
        workspace config's ``insights_synthesizer`` is ``"pattern"``
        (default), OR
      - the v1.0 LLM-driven synthesizer
        (:func:`ee.foresight.insights_llm.synthesize_insights_llm`),
        when the toggle is ``"llm"``. Failures collapse to the pattern
        path so the wire response never 5xxs.

    Engine imports are lazy so the cloud module stays clean of the
    engine layer per the import-linter contract.

    Empty workspaces return ``items=[]`` — the synthesizer yields no
    rows when none of the rules / patterns can fire.

    PR 10 (v1.0): per-persona calibration + rolling accuracy + drift
    now read PredictionRecord docs instead of the v0.5 proxies. The
    ``tier_distribution_deltas`` + ``latest_backtest`` synthesizer
    fields still source from backtest docs (those carry the per-run
    tier mix + gate decision the prediction records don't echo).

    LLM PR (v1.0): the workspace can opt into the LLM synthesizer via
    the ``insights_synthesizer`` config; the wire shape stays the
    same. Cost discipline: LLM mode is opt-in only — the default
    pattern path stays free.
    """
    workspace_id = _require_workspace(ctx)
    now = datetime.now(UTC_TZ)

    # Three reads — paired-only PredictionRecords for the rolling
    # series, full PredictionRecords for the per-persona +
    # confidence-drift + modal-distribution rollups, recent backtest
    # docs for the tier_distribution_deltas + latest_backtest fields.
    paired_records = await _fetch_prediction_records(
        workspace_id,
        window_days=AGGREGATE_DEFAULT_WINDOW_DAYS,
        now=now,
        paired_only=True,
    )
    all_records = await _fetch_prediction_records(
        workspace_id,
        window_days=AGGREGATE_DEFAULT_WINDOW_DAYS,
        now=now,
        paired_only=False,
    )
    backtest_docs = await _fetch_recent_backtest_docs(
        workspace_id,
        window_days=AGGREGATE_DEFAULT_WINDOW_DAYS,
        now=now,
    )

    rolling = _compose_rolling_accuracy_from_records(paired_records, now=now)
    drift = _compose_confidence_drift_from_records(all_records)
    per_persona = _compose_per_persona_calibration(
        all_records,
        floor_threshold=0.50,
    )
    tier_deltas = _compose_tier_distribution_deltas(
        backtest_docs,
        configured_default={"premium": 0.05, "mid": 0.15, "tail": 0.80},
    )
    latest_backtest_doc = backtest_docs[-1] if backtest_docs else None
    latest_gate = _compose_latest_backtest_gate(latest_backtest_doc)

    # Read the per-workspace synthesizer choice. Absent doc → "pattern".
    config_view = await _load_insights_config_view(workspace_id)

    raw_insights: list[Any]
    # Track which synthesizer ACTUALLY produced the wire output so the
    # response envelope can disclose it. Default "pattern" covers the
    # untoggled workspace + the LLM-empty fallback path; only a
    # non-empty LLM run flips this to "llm".
    actual_source: Literal["pattern", "llm"] = "pattern"
    if config_view.synthesizer == "llm":
        # Try LLM. On ANY failure (timeout, malformed output, rate
        # limit, missing backend) fall back to the pattern synthesizer
        # so the wire response stays valid. The LLM helper itself
        # collapses internal exceptions to ``[]``; we treat that as
        # "fallback to pattern" so the operator never sees an empty
        # panel when the pattern rules would have fired.
        llm_insights = await _synthesize_insights_llm(
            workspace_id=workspace_id,
            now=now,
            rolling=rolling,
            drift=drift,
            per_persona=per_persona,
            tier_deltas=tier_deltas,
            latest_gate=latest_gate,
            all_records=all_records,
        )
        if llm_insights:
            raw_insights = list(llm_insights)
            actual_source = "llm"
        else:
            # Empty LLM output — likely a failure or a quiet workspace.
            # Either way the pattern rules are the safe default; if
            # they also produce nothing the response is correctly empty.
            # ``actual_source`` stays "pattern" — the user is reading
            # pattern-synthesizer rows regardless of the config toggle.
            logger.warning(
                "foresight.insights.llm_empty_falling_back_to_pattern",
                extra={"workspace_id": workspace_id},
            )
            raw_insights = list(
                _synthesize_insights_pattern(
                    now=now,
                    rolling=rolling,
                    drift=drift,
                    per_persona=per_persona,
                    tier_deltas=tier_deltas,
                    latest_gate=latest_gate,
                )
            )
    else:
        raw_insights = list(
            _synthesize_insights_pattern(
                now=now,
                rolling=rolling,
                drift=drift,
                per_persona=per_persona,
                tier_deltas=tier_deltas,
                latest_gate=latest_gate,
            )
        )

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
    return InsightsResponse(items=items, synth_source=actual_source)


# ── LLM insights (Team 3 wave 3) ──────────────────────────────────────────
# Workspace-config toggle + LLM synthesizer integration. Team 1's
# scenario CRUD lives elsewhere in this file — do not interleave. All
# helpers below are scoped to the LLM insights surface.
# --------------------------------------------------------------------------

# Module-level handle used by tests to inject a stub LLM backend. None
# means "construct the default :class:`ClaudeCodeBackend`"; tests
# monkey-patch this to a stub backend that returns canned JSON.
_LLM_BACKEND_OVERRIDE: Any | None = None


def _set_llm_backend_for_testing(backend: Any | None) -> None:
    """Test-only hook to inject a stub LLM backend.

    Replaces the default :class:`ClaudeCodeBackend` constructed inside
    :func:`_synthesize_insights_llm`. Tests use this rather than
    monkey-patching ``claude_agent_sdk`` so the cloud module stays
    decoupled from the SDK install.
    """
    global _LLM_BACKEND_OVERRIDE
    _LLM_BACKEND_OVERRIDE = backend


def _synthesize_insights_pattern(
    *,
    now: datetime,
    rolling: Sequence[Any],
    drift: ConfidenceDrift,
    per_persona: Sequence[Any],
    tier_deltas: Sequence[Any],
    latest_gate: Any | None,
) -> list[Any]:
    """Run the v0.5 deterministic five-rule synthesizer over the bundle.

    Lazy engine imports per the cloud → engine import-linter contract.
    Returns the list directly (already sorted + capped by the engine
    function).
    """
    from pocketpaw_ee.foresight.insights import (  # noqa: PLC0415
        ConfidenceDriftInput,
        SynthesizerInput,
        synthesize_insights,
    )
    from pocketpaw_ee.foresight.insights import (  # noqa: PLC0415
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
    return list(synthesize_insights(bundle, cap=INSIGHTS_DEFAULT_CAP))


async def _synthesize_insights_llm(
    *,
    workspace_id: str,
    now: datetime,
    rolling: Sequence[Any],
    drift: ConfidenceDrift,
    per_persona: Sequence[Any],
    tier_deltas: Sequence[Any],
    latest_gate: Any | None,
    all_records: list[Any],
) -> list[Any]:
    """Run the v1.0 LLM-driven synthesizer over the bundle.

    The helper is wrapped in a broad except so any unexpected error
    (engine module import failure, prompt-construction edge case,
    backend not wired) falls back to ``[]`` and the caller swaps to
    the pattern synthesizer. The LLM module itself ALSO collapses
    internal exceptions to ``[]``; this outer guard catches anything
    the inner guard misses (e.g. import errors).
    """
    try:
        # Lazy engine imports per the import-linter contract.
        from pocketpaw_ee.foresight.insights import (  # noqa: PLC0415
            ConfidenceDriftInput,
        )
        from pocketpaw_ee.foresight.insights import (  # noqa: PLC0415
            RollingAccuracyPoint as _RollingPoint,
        )
        from pocketpaw_ee.foresight.insights_llm import (  # noqa: PLC0415
            LLMInsightsInput,
            RecentPredictionRecordSummary,
            synthesize_insights_llm,
        )

        backend = _LLM_BACKEND_OVERRIDE
        if backend is None:
            from pocketpaw_ee.foresight.llm.adapter import (  # noqa: PLC0415
                ClaudeCodeBackend,
            )

            backend = ClaudeCodeBackend()

        period_key = _llm_period_key_for(now)
        recent_records = [
            RecentPredictionRecordSummary(
                anchor_id=getattr(doc, "anchor_id", "") or "",
                persona_id=getattr(doc, "persona_id", "") or "",
                modal_outcome=_extract_modal_outcome(getattr(doc, "prediction", {})),
                confidence=float(getattr(doc, "confidence", 0.0) or 0.0),
                paired=bool(getattr(doc, "paired", False)),
                observed_outcome=_extract_modal_outcome(
                    getattr(doc, "observed_outcome", None) or {}
                )
                or None,
                captured_at=getattr(doc, "captured_at", None),
            )
            for doc in all_records[-50:]
        ]
        bundle = LLMInsightsInput(
            workspace_id=workspace_id,
            period_key=period_key,
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
            recent_records=tuple(recent_records),
        )
        result = await synthesize_insights_llm(
            bundle,
            backend,
            cap=INSIGHTS_DEFAULT_CAP,
        )
        return list(result)
    except Exception as exc:  # noqa: BLE001 — defensive outer guard
        logger.warning(
            "foresight.insights.llm_outer_error",
            extra={"workspace_id": workspace_id, "error": repr(exc)},
        )
        return []


def _llm_period_key_for(now: datetime) -> str:
    """Compute a stable ISO year-week key for ``now``.

    Mirrors :func:`ee.foresight.insights._default_period_key`. Kept
    local rather than imported so the cloud → engine call surface
    stays restricted to the public synthesizer functions (the helper
    is private to the engine module).
    """
    iso = now.isocalendar()
    return f"{iso.year}_W{iso.week:02d}"


def _extract_modal_outcome(payload: Any) -> str:
    """Pull a short modal-outcome label out of a prediction or
    observation payload dict. Returns ``""`` when nothing matches.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("modal_outcome", "outcome", "action", "decision"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:48]
    return ""


# --------------------------------------------------------------------------
# Insights-synthesizer config — workspace-scoped GET / PUT.
# --------------------------------------------------------------------------


async def _load_insights_config_view(workspace_id: str) -> InsightsConfigView:
    """Compose the workspace's insights-synthesizer config view.

    Absent doc OR a doc missing the field (older records persisted
    before this PR) collapses to the default ``"pattern"`` synthesizer
    so the read path stays back-compat.
    """
    # Lazy import — keeps the LLM cache-TTL constant out of the cloud
    # module's static deps. ``ee.foresight.insights_llm`` is in the
    # engine namespace; lazy import preserves the import-linter
    # contract.
    from pocketpaw_ee.foresight.insights_llm import (  # noqa: PLC0415
        DEFAULT_CACHE_TTL_SECONDS,
    )

    doc = await _ForesightWorkspaceConfigDoc.find_one(
        {"workspace": workspace_id},
    )
    if doc is None:
        return InsightsConfigView(
            workspace_id=workspace_id,
            synthesizer="pattern",
            llm_cache_ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
            updated_at=None,
        )
    # Older docs persisted before this PR don't carry the field; the
    # Pydantic default makes ``doc.insights_synthesizer`` resolve to
    # ``"pattern"``, but be defensive in case a malformed write
    # landed.
    synthesizer = getattr(doc, "insights_synthesizer", "pattern") or "pattern"
    if synthesizer not in ("pattern", "llm"):
        synthesizer = "pattern"
    return InsightsConfigView(
        workspace_id=workspace_id,
        synthesizer=synthesizer,  # type: ignore[arg-type]
        llm_cache_ttl_seconds=DEFAULT_CACHE_TTL_SECONDS,
        updated_at=doc.updatedAt,
    )


def _to_insights_config_response(view: InsightsConfigView) -> ForesightInsightsConfigResponse:
    """Map the domain view to the wire DTO."""
    return ForesightInsightsConfigResponse(
        workspace_id=view.workspace_id,
        synthesizer=view.synthesizer,
        llm_cache_ttl_seconds=view.llm_cache_ttl_seconds,
        updated_at=iso_utc(view.updated_at),
    )


async def get_insights_config(ctx: RequestContext) -> ForesightInsightsConfigResponse:
    """Return the workspace's resolved insights-synthesizer config view.

    GET /api/v1/foresight/workspace/insights-config backing.

    Tenancy:
      - 403 ``foresight.no_workspace`` when the caller has no active
        workspace.
      - No cross-tenant reads possible — the view is intrinsically
        per-workspace; an absent config collapses to the default view
        (synthesizer="pattern"). Never 404.
    """
    workspace_id = _require_workspace(ctx)
    # no-event: read-only path; emit only on writes (cloud rule #9).
    view = await _load_insights_config_view(workspace_id)
    return _to_insights_config_response(view)


async def set_insights_config(
    ctx: RequestContext,
    body: SetForesightInsightsConfigRequest,
) -> ForesightInsightsConfigResponse:
    """Upsert the workspace's insights-synthesizer choice.

    PUT /api/v1/foresight/workspace/insights-config backing.

    Emit semantics:
      - When the effective synthesizer changes (pattern → llm or
        llm → pattern), fire ``foresight.insights_config.updated``
        with ``data={workspace_id, synthesizer (new), previous_synthesizer}``
        so listeners (LLM cache invalidator, UI panels) can react
        without a round trip.
      - A no-op write (same value as the current effective synthesizer)
        does NOT emit — keeps the UI's optimistic-local-state path
        from rebroadcasting redundant updates.
    """
    body = SetForesightInsightsConfigRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)

    previous_view = await _load_insights_config_view(workspace_id)
    new_choice = body.synthesizer

    # Upsert pattern — Beanie has no native upsert helper that returns
    # the resulting doc, so split into find_one + insert / update. The
    # workspace field is unique-indexed so a concurrent insert race
    # would surface as a DuplicateKeyError; this admin-only write path
    # treats that race as a 500 (no retry loop).
    doc = await _ForesightWorkspaceConfigDoc.find_one(
        {"workspace": workspace_id},
    )
    if doc is None:
        doc = _ForesightWorkspaceConfigDoc(
            workspace=workspace_id,
            insights_synthesizer=new_choice,
        )
        await doc.insert()
    else:
        doc.insights_synthesizer = new_choice
        await doc.save()

    new_view = await _load_insights_config_view(workspace_id)
    response = _to_insights_config_response(new_view)

    if new_view.synthesizer != previous_view.synthesizer:
        await emit(
            ForesightInsightsConfigUpdated(
                data={
                    "workspace_id": workspace_id,
                    "synthesizer": new_view.synthesizer,
                    "previous_synthesizer": previous_view.synthesizer,
                }
            )
        )
    # else: no-event: idempotent write (cloud rule #9 allows this with
    # the inline comment); the GET still returns the resolved view so
    # the client's PUT round-trip stays useful.
    return response


# ── end LLM insights (Team 3 wave 3) ─────────────────────────────────────


# ---------------------------------------------------------------------------
# Live snapshot (RFC 08 §11.3) — GET /api/v1/foresight/runs/{id}/live-snapshot.
#
# Backs the paw-enterprise LivePanel. Composes a compact "right now"
# view of a run from the persisted ForesightRun doc + the run's
# projection list. Cross-tenant 404 via ``_fetch_in_workspace`` so the
# existence-leak rule that governs the other run-scoped endpoints
# applies here too.
#
# Three data sources, three derivations:
#
#   - ``status`` / ``generated_at`` / ``run_id`` — read off the run doc.
#     The wire vocabulary renames ``queued`` → ``created`` to match the
#     paw-enterprise contract; everything else mirrors the persisted
#     status one-to-one.
#   - ``tier_mix_actual`` — derived from ``run.result.tier_distribution``
#     when the engine reported one (the run completed and used a tier
#     pool). Empty / in-flight runs land in the zero-triple branch.
#   - ``sampled_traces`` — deterministic slice of the persisted
#     :class:`ForesightProjectedDecision` rows. Sub-type-aware
#     formatter mirrors the Instinct bridge labelling so the operator
#     sees consistent text across the Tray + LivePanel.
#   - ``anomalies`` — three v1.0 rules (tier_drift, confidence_spike,
#     stalled_persona) evaluated by ``ee.cloud.foresight.live_snapshot``.
#     Pure functions, easy to unit-test, easy to extend in v1.1.
# ---------------------------------------------------------------------------


# Status vocabulary the LivePanel speaks (paw-enterprise PR #267).
# Maps the persisted ``ScenarioRunStatus`` shape to the wire status set
# the UI expects. v0.5 calls the initial state ``queued``; PR #267
# calls the same state ``created``. The mapping stays here rather than
# on the wire DTO so the storage shape can keep its v0.5 vocabulary
# uniform across the foresight surface.
_LIVE_STATUS_MAP: dict[str, str] = {
    "queued": "created",
    "running": "running",
    "complete": "complete",
    "failed": "failed",
}


def _live_status_for(persisted_status: str) -> str:
    """Map a persisted run status to the LivePanel wire vocabulary."""
    return _LIVE_STATUS_MAP.get(persisted_status, "created")


def _expected_persona_ids(run_request: dict[str, Any] | None) -> list[str]:
    """Extract the persona name set the run was created with.

    Used by the silent-persona ``critical`` detector — knowing the
    full roster lets us flag personas with ZERO projections (not just
    behind by 30s). v0.5 stores the request body verbatim under
    ``ForesightRun.request`` so the persona names are recoverable
    without re-running the engine.
    """
    if not isinstance(run_request, dict):
        return []
    personas = run_request.get("personas") or []
    if not isinstance(personas, list):
        return []
    out: list[str] = []
    for entry in personas:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            out.append(name)
    return out


def _live_view_to_response(view: LiveSnapshotView) -> LiveSnapshotResponse:
    """Map the frozen-dataclass view onto the wire DTO.

    Done by hand because the nested tuple-of-domain → list-of-pydantic
    projection benefits from explicit shape mapping; every nested DTO
    uses ``extra='forbid'`` so the Pydantic round-trip would refuse
    superfluous keys anyway.
    """
    return LiveSnapshotResponse(
        run_id=view.run_id,
        generated_at=_iso_required(view.generated_at),
        status=view.status,
        tier_mix_actual=TierMixActual(
            premium=view.tier_mix_actual.premium,
            mid=view.tier_mix_actual.mid,
            tail=view.tier_mix_actual.tail,
        ),
        sampled_traces=[
            SampledTrace(
                tick_id=t.tick_id,
                persona_id=t.persona_id,
                sub_type=t.sub_type,
                action_summary=t.action_summary,
                confidence=t.confidence,
            )
            for t in view.sampled_traces
        ],
        anomalies=[Anomaly(kind=a.kind, severity=a.severity, body=a.body) for a in view.anomalies],
    )


async def get_live_snapshot(
    ctx: RequestContext,
    run_id: str,
) -> LiveSnapshotResponse:
    """Compose the live snapshot for one Foresight run.

    Workspace-scoped: an unknown / cross-tenant run id collapses to a
    404 via :func:`_fetch_in_workspace` — the same existence-not-
    leakable rule the other run-scoped reads enforce.

    Read-only — no event emit, no Beanie writes (cloud rule #9).

    The wire shape is locked to paw-enterprise PR #267; every field
    name and nesting shape on :class:`LiveSnapshotResponse` mirrors
    the TypeScript surface the LivePanel was built against.

    Empty / in-flight runs collapse to a zero ``tier_mix_actual``
    triple and an empty ``sampled_traces`` array — the UI's empty
    state renders directly off those defaults without a separate 404
    branch.
    """
    workspace_id = _require_workspace(ctx)

    # 404-collapse first — an unknown run id must not leak a tier-mix
    # or anomaly readout that could probe existence.
    run_doc = await _fetch_in_workspace(workspace_id, run_id)

    # Read the projection list for the run. Bounded — a run with a
    # huge fanout would still serve a sub-cap slice. We pull a wide
    # window (up to 500 rows, the projection list's hard cap) so the
    # anomaly detectors and the sampler have a representative
    # population without dragging the entire collection into memory.
    projection_docs = (
        await _ForesightProjectedDecisionDoc.find({"workspace": workspace_id, "run_id": run_id})
        .sort([("tick_id", 1), ("anchor_id", 1)])  # type: ignore[list-item]
        .limit(500)
        .to_list()
    )
    projection_domains = [_to_projected_decision_domain(d) for d in projection_docs]

    # Tier mix — driven from the engine's tier_distribution when the
    # run reported one (completed runs with a tier pool); otherwise the
    # zero triple. The fall-through path is also empty-runs / in-flight
    # — the UI's LivePanel renders the empty state from zeros.
    result_blob = dict(run_doc.result or {}) if run_doc.result else {}
    tier_mix = derive_tier_mix_actual(
        projections=projection_domains,
        run_result=result_blob,
    )

    # Sampled traces — deterministic slice of up to 10 projections.
    sampled = sample_traces(projection_domains)

    # Anomaly detection — three rules + the silent-persona check.
    # ``latest_tick_id`` is the max tick id seen across projections;
    # ``latest_tick_ts`` is the corresponding ``createdAt`` (the run
    # doesn't carry a per-tick timestamp on the doc itself in v0.5,
    # so we derive it from the most recent projection's createdAt).
    latest_tick_id: int | None = None
    latest_tick_ts: datetime | None = None
    if projection_domains:
        latest = max(projection_domains, key=lambda p: p.tick_id)
        latest_tick_id = latest.tick_id
        latest_tick_ts = latest.created_at
    anomalies: list[LiveAnomaly] = detect_all_anomalies(
        tier_mix_actual=tier_mix,
        projections=projection_domains,
        expected_persona_ids=_expected_persona_ids(dict(run_doc.request or {})),
        latest_tick_id=latest_tick_id,
        latest_tick_ts=latest_tick_ts,
        configured_mix=_LIVE_DEFAULT_TIER_MIX,
    )

    view = LiveSnapshotView(
        run_id=run_id,
        generated_at=datetime.now(UTC_TZ),
        status=_live_status_for(run_doc.status),  # type: ignore[arg-type]
        tier_mix_actual=tier_mix,
        sampled_traces=tuple(sampled),
        anomalies=tuple(anomalies),
    )
    return _live_view_to_response(view)


__all__ = [
    "AGGREGATE_DEFAULT_WINDOW_DAYS",
    "AGGREGATE_MAX_WINDOW_DAYS",
    "GATE_DEFAULT_THRESHOLD",
    "INSIGHTS_DEFAULT_CAP",
    "create_backtest",
    "create_scenario_run",
    "emit_prediction_record",
    "emit_projected_decision",
    "get_aggregate_rollup",
    "get_backtest",
    "get_insights",
    "get_insights_config",
    "get_live_snapshot",
    "get_onboarding_gate",
    "get_scenario_run",
    "get_threshold",
    "list_backtests",
    "list_instinct_proposals_for_run",
    "list_projected_decisions",
    "list_scenario_runs",
    "list_scenarios",
    "pair_prediction",
    "set_insights_config",
    "set_threshold",
]
