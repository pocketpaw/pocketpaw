# ee/pocketpaw_ee/cloud/foresight/domain.py
# Updated: 2026-05-26 (feat/foresight-v10-insights-llm) — RFC 08 v1.0
# adds ``InsightsConfigView`` for the per-workspace LLM-vs-pattern
# synthesizer toggle. Workspace-scoped at construction per cloud rule
# #3 — ``workspace_id`` is required positionally. The view mirrors the
# DTO field-for-field so the service maps via Pydantic per cloud rule #8.
# Updated: 2026-05-26 (feat/foresight-v10-scenario-editor-backend) — RFC 08
# v1.0 wave 3 — adds workspace-scoped custom-scenario domain shapes:
#   - ``CustomScenario`` — frozen value object mirroring the persisted
#     :class:`pocketpaw_ee.cloud.models.foresight_workspace_scenario.ForesightWorkspaceScenario`
#     document 1-to-1 plus the cloud rule #3 tenancy invariant.
#   - ``CustomScenarioParsedMeta`` — frozen value object capturing the
#     denormalized parse result (num_personas, num_ticks, tier_mix,
#     precedent_seed) the service stamps onto each doc at write time so
#     the list endpoint can render per-row counts without re-parsing the
#     YAML body on every request.
#   The shapes are frozen so the service can hand them to mapping
#   helpers without import-direction violations — both sides see plain
#   dataclasses with no Beanie / FastAPI surface.
# Updated: 2026-05-26 (feat/foresight-v10-live-snapshot-and-fixes) — RFC
# 08 v1.0 — adds live-snapshot domain shapes:
#   - ``LiveSnapshotView`` — workspace-scoped frozen view backing the
#     ``GET /runs/{id}/live-snapshot`` endpoint.
#   - ``LiveTierMixActual`` — premium/mid/tail share triple.
#   - ``LiveSampledTrace`` — one sampled per-tick projection row.
#   - ``LiveAnomaly`` — one anomaly flagged by the detector rules.
#   The view is implicitly workspace-scoped via the service call that
#   produced it; the service always passes through the tenant filter
#   before composing the view (cloud rule #3 invariant held there).
# Updated: 2026-05-26 (feat/foresight-v10-prediction-record-persist) — RFC
# 08 v1.0 PR 10:
#   - Added ``PredictionRecord`` frozen dataclass mirroring the persisted
#     :class:`pocketpaw_ee.cloud.models.foresight_prediction_record.ForesightPredictionRecord`
#     document 1-to-1 plus the cloud-rule-#3 tenancy invariant. The
#     service layer maps this to the Mongo doc and back; the aggregate +
#     insights endpoints read these records (filtering ``paired=True``
#     for rolling-accuracy buckets) instead of the v0.5 backtest +
#     projected-decision proxies.
#   - The shape is frozen so the cloud service can hand it to the
#     engine-side aggregator primitives (``ee.foresight.aggregator``)
#     without import-direction violations — both sides see plain
#     dataclasses with no Beanie / FastAPI / pydantic surface.
# Updated: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision) —
# PR 5:
#   - Rebuilt ``ProjectedDecision`` to match the persisted shape of
#     the new ``foresight_projected_decisions`` collection. Fields
#     follow RFC §7.7 (anchor_id, persona_id, tick_id, decision_text,
#     confidence, sub_type, run_id) plus the workspace tenancy key.
#   - Added ``forward_precedent_decision_id`` field stubbed to None —
#     RFC 07 Decision Graph wiring (forward-precedent edge) is out of
#     scope per the PR brief; the field is reserved so the future
#     backfill pass doesn't have to reshape the wire contract.
#   - The PR 7 embedded-decision shape didn't survive any consumers
#     beyond the docstring, so the rewrite is non-breaking. The
#     dataclass is still frozen and workspace_id is still required
#     positionally per cloud rule #3.
# Updated: 2026-05-25 (feat/foresight-v04-backtest-aggregator) — PR 4:
#   - Added ``BacktestRun`` (parallel to ``ScenarioRun`` but for
#     retroactive runs scored against ground truth) and
#     ``OnboardingGateState`` (the workspace's unlock posture derived
#     from the latest passing backtest). Both enforce the cloud rule #3
#     tenancy invariant — ``workspace_id`` is required positionally.
# Created: 2026-05-25 (feat/foresight-v07-cloud-mount) — RFC 08 PR 7.
#
# Foresight cloud domain — frozen value objects, no Beanie / Pydantic /
# FastAPI imports. The service (``ee.cloud.foresight.service``) maps
# between these and the ``ForesightRun`` / ``ForesightBacktest`` /
# ``ForesightProjectedDecision`` Beanie documents; the DTO layer
# (``ee.cloud.foresight.dto``) maps these to Pydantic responses.
#
# Multi-tenancy is enforced at construction per the cloud rule #3:
# ``workspace_id`` is required positionally with no default — building a
# ``ScenarioRun`` (or ``BacktestRun``, ``OnboardingGateState``,
# ``ProjectedDecision``) without one is a type error.
#
# Five value objects ship as of PR 5:
#
#   - ``ScenarioRun`` (PR 7) — the persisted forward-run record.
#   - ``ProjectedDecision`` (PR 5) — per-anchor projection record
#     persisted into ``foresight_projected_decisions``.
#   - ``BacktestRun`` (PR 4) — persisted retroactive-run record with
#     the aggregator's accuracy summary + threshold decision pinned
#     to the doc so historical pass/fail labels survive default tuning.
#   - ``OnboardingGateState`` (PR 4) — derived state served by
#     ``GET /api/v1/foresight/onboarding/gate``; carries the unlock
#     boolean + last passing backtest reference + observed accuracy.

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
    """One projected decision emitted during a Foresight run.

    Field shape mirrors the persisted
    :class:`pocketpaw_ee.cloud.models.foresight_projected_decision.ForesightProjectedDecision`
    document 1-to-1 plus the cloud rule #3 tenancy invariant:

    - ``id`` — Mongo ObjectId rendered as hex string.
    - ``workspace_id`` — tenancy key (required positionally).
    - ``run_id`` — the ForesightRun document id this projection
      belongs to.
    - ``anchor_id`` — sub-type-specific anchor identifier
      (``decision:<name>`` / ``segment:<role>`` / ``rollout:<event>``).
    - ``persona_id`` — the persona whose modal action drove the
      projection (empty string when no persona acted).
    - ``tick_id`` — zero-based tick index inside the run.
    - ``decision_text`` — short string capturing the modal action
      verb (e.g. ``"accept"``, ``"churn"``, ``"escalate"``).
    - ``confidence`` — aggregate confidence in (0.0, 1.0).
    - ``sub_type`` — the scenario's sub_type.
    - ``forward_precedent_decision_id`` — RFC §7.7 forward-precedent
      hook. ``None`` in PR 5 because RFC 07's Decision Graph wiring
      isn't yet in pocketpaw; the field is reserved so the future
      backfill pass (Decision Graph → projection cross-link) doesn't
      have to reshape the wire contract.
    - ``created_at`` — server-side timestamp from the Mongo doc.
    """

    id: str
    workspace_id: str
    run_id: str
    anchor_id: str
    tick_id: int
    decision_text: str
    confidence: float
    sub_type: str
    persona_id: str = ""
    forward_precedent_decision_id: str | None = None
    created_at: datetime | None = None


BacktestRunStatus = Literal["queued", "running", "complete", "failed"]


@dataclass(frozen=True)
class PredictionRecord:
    """One projected outcome held until reality lands.

    Persisted equivalent of :class:`ee.foresight.calibration.PredictionRecord`
    (engine in-memory shape) — the cloud's Mongo-backed mirror with the
    cloud-rule-#3 tenancy invariant. Fields mirror
    :class:`pocketpaw_ee.cloud.models.foresight_prediction_record.ForesightPredictionRecord`
    1-to-1:

    - ``id`` — Mongo ObjectId rendered as hex string.
    - ``workspace_id`` — tenancy key (required positionally).
    - ``anchor_id`` — sub-type-specific anchor identifier
      (``decision:<name>`` / ``segment:<role>`` / ``rollout:<event>``).
    - ``persona_id`` — the persona whose modal action drove the
      projection (empty string when no persona acted).
    - ``scenario_id`` — scenario name / template identifier.
    - ``run_id`` — the ForesightRun / ForesightBacktest doc id.
    - ``tick_id`` — zero-based tick index inside the run.
    - ``prediction`` — projected-outcome payload (JSON dict).
    - ``confidence`` — aggregate confidence in (0.0, 1.0).
    - ``captured_at`` — server-side timestamp when the engine emitted
      this prediction.
    - ``observed_at`` — timestamp when reality landed (``None`` while
      unpaired).
    - ``observed_outcome`` — the actual outcome dict (``None`` while
      unpaired).
    - ``paired`` — ``True`` once observation lands. The §11.5
      rolling-accuracy read filters on this so unpaired projections
      never inflate the denominator.
    - ``pair_delta`` — per-metric diff dict (``None`` until paired).
    """

    id: str
    workspace_id: str
    captured_at: datetime
    anchor_id: str = ""
    persona_id: str = ""
    scenario_id: str = ""
    run_id: str = ""
    tick_id: int = 0
    prediction: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    observed_at: datetime | None = None
    observed_outcome: dict[str, Any] | None = None
    paired: bool = False
    pair_delta: dict[str, Any] | None = None


@dataclass(frozen=True)
class BacktestRun:
    """One retroactive backtest run, scoped to a workspace.

    Parallel to :class:`ScenarioRun` but with two extra fields the
    forward-run path doesn't need:

    - ``gate_decision``: the ``ThresholdDecision.as_wire_dict()`` the
      aggregator produced when the run completed. Drives the onboarding
      gate (``GET /foresight/onboarding/gate``) and is persisted into
      the Mongo doc so the unlock label is stable across queries.
    - ``threshold``: the gate threshold this run was scored against,
      captured at completion time so a future bump of the default cap
      doesn't retroactively flip historical pass/fail labels.

    Like ``ScenarioRun``, ``id`` is the Mongo ObjectId rendered as a hex
    string; ``request`` is the validated POST body; ``result`` is the
    engine + aggregator combined wire dict; ``error`` is the failure
    message string when the run raises.
    """

    id: str
    workspace_id: str
    scenario_name: str
    status: BacktestRunStatus
    created_at: datetime
    request: dict[str, Any]
    threshold: float
    result: dict[str, Any] | None = None
    gate_decision: dict[str, Any] | None = None
    error: str | None = None
    created_by: str = ""
    updated_at: datetime | None = None


@dataclass(frozen=True)
class OnboardingGateState:
    """The workspace's forward-sim unlock posture (RFC §13.1 gate 7).

    Derived from the most recent completed :class:`BacktestRun` in the
    workspace. Fields:

    - ``unlocked``: ``True`` when the latest passing backtest cleared
      the gate threshold; ``False`` when no backtest has run yet, the
      latest one failed, or the latest one is still in flight.
    - ``threshold``: the workspace's effective gate threshold (the
      default :data:`pocketpaw_ee.cloud.foresight.service.GATE_DEFAULT_THRESHOLD`
      in v0.1; v1.0 reads a workspace-config override).
    - ``last_backtest_id``: the id of the most recent completed
      backtest, or ``None`` if no backtest has run.
    - ``last_backtest_accuracy``: the modal accuracy of that backtest,
      or ``None`` when ``last_backtest_id`` is ``None``.
    - ``last_backtest_at``: the completion timestamp of that backtest.
    - ``reason``: short string the UI can render to explain a closed
      gate (``"no_backtest" | "below_threshold" | "in_flight" | "unlocked"``).
    """

    workspace_id: str
    unlocked: bool
    threshold: float
    reason: Literal["no_backtest", "below_threshold", "in_flight", "unlocked"]
    last_backtest_id: str | None = None
    last_backtest_accuracy: float | None = None
    last_backtest_at: datetime | None = None


# ---------------------------------------------------------------------------
# Scenario catalog (RFC §11.2) — bundled YAML template descriptors.
#
# Catalog entries are global, not workspace-scoped — they describe the
# static set of templates shipped with the engine. The cloud rule #3
# tenancy invariant doesn't apply here (no Mongo doc, no tenant key);
# the descriptor mirrors the YAML on disk.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioCatalogEntry:
    """One scenario template descriptor surfaced by ``GET /scenarios``.

    Field-for-field mirror of :class:`ScenarioCatalogItem` so the
    service layer can map domain → DTO via Pydantic's
    ``model_validate(..., from_attributes=True)`` per cloud rule #8.

    ``tier_mix`` carries the explicit 5/15/80 default (or whatever
    override the YAML declares) as a plain dict — easier for the
    frontend to consume than a triple of floats.
    """

    id: str
    name: str
    sub_type: str
    description: str
    num_personas: int
    num_ticks: int
    tier_mix: dict[str, float]


# ---------------------------------------------------------------------------
# Aggregate rollup (RFC §11.5) — derived view over recent backtests +
# projection records. Workspace-scoped at construction per cloud rule #3.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollingAccuracyPoint:
    """One time-bucketed accuracy reading on the rolling series."""

    ts: datetime
    accuracy: float
    sample_count: int


@dataclass(frozen=True)
class ConfidenceDrift:
    """Confidence-drift summary across the rollup window.

    ``trend`` uses the §11.5 vocabulary (``"rising"`` / ``"falling"``
    / ``"flat"``); ``magnitude`` is the absolute drift size.
    """

    trend: Literal["rising", "falling", "flat"]
    magnitude: float


@dataclass(frozen=True)
class ModalOutcomeEntry:
    """One row in the modal-outcome distribution."""

    outcome: str
    share: float


@dataclass(frozen=True)
class AggregateRollup:
    """Workspace-scoped aggregate rollup over a trailing window.

    Reads come from the persisted backtest + projection collections; no
    new collection is introduced for the rollup itself in v0.1
    (computed on demand). The cloud rule #3 invariant holds:
    ``workspace_id`` is positionally required.
    """

    workspace_id: str
    window_days: int
    generated_at: datetime
    rolling_accuracy: tuple[RollingAccuracyPoint, ...]
    confidence_drift: ConfidenceDrift
    modal_outcome_distribution: tuple[ModalOutcomeEntry, ...]


# ---------------------------------------------------------------------------
# Insights (RFC §11.6) — synthesizer output container. Domain mirror of
# the wire shape so the service can compose ``InsightView`` -> DTO via
# Pydantic mapping per cloud rule #8.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InsightView:
    """One insight row in the workspace's Insights panel.

    Tenancy: each view is implicitly workspace-scoped via the service
    call that produced it — the row itself is not persisted in v0.1
    (the synthesizer re-runs on every poll), so it doesn't carry the
    ``workspace_id`` field that a persisted entity would. The cloud
    rule #3 invariant still holds: the entity that constructs this
    view (``get_insights``) always passes through the tenant filter.
    """

    id: str
    kind: str
    title: str
    body: str
    severity: Literal["info", "warning", "critical"]
    anchor_refs: tuple[str, ...]
    generated_at: datetime


# ---------------------------------------------------------------------------
# Live snapshot (RFC 08 §11.3) — workspace-scoped view backing
# ``GET /api/v1/foresight/runs/{id}/live-snapshot``. Cloud rule #3 is
# enforced at the service-call site rather than on the dataclass (the
# view is read-only and ephemeral — it's recomputed on every request,
# so there's no persisted row that could leak across tenants by
# storage path).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveTierMixActual:
    """The premium/mid/tail share triple observed across the run.

    Empty runs collapse to zeros; the service never raises when no
    projections have landed yet (the UI's LivePanel renders the empty
    state from the zeros).
    """

    premium: float
    mid: float
    tail: float


@dataclass(frozen=True)
class LiveSampledTrace:
    """One sampled per-tick projection trace, deterministic at fetch time."""

    tick_id: int
    persona_id: str
    sub_type: str
    action_summary: str
    confidence: float


@dataclass(frozen=True)
class LiveAnomaly:
    """One anomaly flagged on the run snapshot.

    Severity vocabulary mirrors the §11.6 insights surface so the UI's
    severity → colour mapping is uniform across panels.
    """

    kind: Literal["tier_drift", "confidence_spike", "stalled_persona"]
    severity: Literal["info", "warning", "critical"]
    body: str


@dataclass(frozen=True)
class LiveSnapshotView:
    """Compact view of one Foresight run's live state.

    Tenancy: implicitly workspace-scoped via the service call that
    produced this view — the service always passes through the tenant
    filter before composing the snapshot. The view itself is not
    persisted (recomputed on every request) so it carries no
    ``workspace_id`` field of its own.

    ``status`` mirrors :class:`ScenarioRunStatus` but renames
    ``queued`` to ``created`` to match the paw-enterprise PR #267
    contract — the v0.5 wire vocabulary uses ``queued``; the LivePanel
    spec calls the same state ``created``. The service maps between
    them so the wire surface and the persisted shape stay decoupled.
    """

    run_id: str
    generated_at: datetime
    status: Literal["created", "running", "complete", "failed"]
    tier_mix_actual: LiveTierMixActual
    sampled_traces: tuple[LiveSampledTrace, ...]
    anomalies: tuple[LiveAnomaly, ...]


# ---------------------------------------------------------------------------
# Per-workspace threshold-override view (RFC 08 v1.0 PR 10).
#
# Workspace-scoped at construction per cloud rule #3 — ``workspace_id`` is
# required positionally with no default. The view is derived state
# (recomputed on every GET / PUT) rather than a persisted snapshot; the
# Mongo doc carries only the override value, the service composes this
# view by reading the doc + the GATE_DEFAULT_THRESHOLD constant.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdOverrideView:
    """The workspace's resolved foresight threshold view.

    Composed by the service from
    :class:`pocketpaw_ee.cloud.models.foresight_workspace_config.ForesightWorkspaceConfig`
    plus the global default constant. Mirrors
    :class:`pocketpaw_ee.cloud.foresight.dto.ForesightThresholdResponse`
    field-for-field so the service maps the two via Pydantic's
    ``model_validate(..., from_attributes=True)`` per cloud rule #8.

    Fields:
      - ``workspace_id``: tenancy key (required positional per cloud rule #3).
      - ``current_threshold``: the effective threshold — override when
        set, default otherwise.
      - ``default_threshold``: echoed default so the UI doesn't hard-code
        the constant.
      - ``is_overridden``: True when a per-workspace override exists.
      - ``updated_at``: when the override was last written; None when
        no override exists.
    """

    workspace_id: str
    current_threshold: float
    default_threshold: float
    is_overridden: bool
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Per-workspace insights-synthesizer config view (RFC 08 v1.0 — LLM
# insights PR). Workspace-scoped at construction per cloud rule #3.
# Mirrors :class:`pocketpaw_ee.cloud.foresight.dto.ForesightInsightsConfigResponse`
# field-for-field so the service maps via Pydantic's
# ``model_validate(..., from_attributes=True)`` per cloud rule #8.
#
# Custom scenarios (RFC 08 v1.0 wave 3) — workspace-owned scenario YAML
# storage. Sibling shape to ``ScenarioCatalogEntry`` (which enumerates
# the bundled engine templates); ``CustomScenario`` is the persisted
# operator-authored record.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InsightsConfigView:
    """The workspace's resolved insights-synthesizer configuration.

    Composed by the service from
    :class:`pocketpaw_ee.cloud.models.foresight_workspace_config.ForesightWorkspaceConfig`
    plus the module constants from ``ee.foresight.insights_llm``. The
    view is derived (recomputed on every GET / PUT) rather than a
    persisted snapshot.

    Fields:
      - ``workspace_id``: tenancy key (required positional per cloud
        rule #3).
      - ``synthesizer``: ``"pattern"`` (the v0.5 deterministic five-rule
        synthesizer; default) or ``"llm"`` (the v1.0 LLM-driven
        synthesizer with a hard fallback to ``pattern`` on LLM failure).
      - ``llm_cache_ttl_seconds``: the in-memory LRU TTL for the LLM
        synthesizer. v1.0 echoes the module constant; v1.1 will expose
        it as a per-workspace override.
      - ``updated_at``: when the config row was last written; None when
        no row exists.
    """

    workspace_id: str
    synthesizer: Literal["pattern", "llm"]
    llm_cache_ttl_seconds: int
    updated_at: datetime | None = None


@dataclass(frozen=True)
class CustomScenarioParsedMeta:
    """Denormalized parse result for a workspace's custom scenario.

    Service stamps this onto the doc at write time so the list endpoint
    can render ``num_personas`` / ``num_ticks`` / ``tier_mix`` per row
    without re-parsing the YAML body on every read. Mirrors the
    ``parsed_meta`` field on
    :class:`pocketpaw_ee.cloud.models.foresight_workspace_scenario.ForesightWorkspaceScenario`.

    ``tier_mix`` carries the premium/mid/tail share triple parsed from
    the YAML (or the captain-locked default {0.05, 0.15, 0.80} when the
    YAML omits the block). ``precedent_seed`` is the scenario-root seed
    if present, else ``None``.
    """

    num_personas: int
    num_ticks: int
    tier_mix: dict[str, float]
    precedent_seed: str | None = None


@dataclass(frozen=True)
class CustomScenario:
    """One workspace-scoped custom scenario, scoped to a workspace.

    Fields mirror the
    :class:`pocketpaw_ee.cloud.models.foresight_workspace_scenario.ForesightWorkspaceScenario`
    document 1-to-1 plus the cloud rule #3 tenancy invariant —
    ``workspace_id`` is required positionally with no default.

    The wire response (``CustomScenarioResponse``) drops ``yaml_body``
    from the list shape but keeps it on the detail shape; this domain
    object carries the full body so the service has a single value
    object spanning both read shapes.
    """

    id: str
    workspace_id: str
    name: str
    sub_type: str
    description: str
    author: str
    created_at: datetime
    updated_at: datetime
    yaml_body: str
    parsed_meta: CustomScenarioParsedMeta


__all__ = [
    "AggregateRollup",
    "BacktestRun",
    "BacktestRunStatus",
    "ConfidenceDrift",
    "CustomScenario",
    "CustomScenarioParsedMeta",
    "InsightView",
    "InsightsConfigView",
    "LiveAnomaly",
    "LiveSampledTrace",
    "LiveSnapshotView",
    "LiveTierMixActual",
    "ModalOutcomeEntry",
    "OnboardingGateState",
    "PredictionRecord",
    "ProjectedDecision",
    "RollingAccuracyPoint",
    "ScenarioCatalogEntry",
    "ScenarioRun",
    "ScenarioRunStatus",
    "ThresholdOverrideView",
]
