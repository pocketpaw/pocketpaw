# ee/pocketpaw_ee/cloud/foresight/dto.py
# Modified: 2026-05-29 (feat/foresight-rehearsals-joined) — v2 landing
# card hydration. Adds the joined rehearsals surface:
#   - ``RehearsalLastRun`` — compact summary of the most recent run
#     (id / status / ran_at / verdict_summary) embedded inline on each
#     list item so the v2 landing card can render without an N+1.
#   - ``RehearsalListItem`` — one card on the v2 ``/foresight`` landing.
#     Mirrors the existing ``CustomScenarioListItem`` field set plus
#     ``run_count`` + optional ``last_run`` for the "draft vs. ran"
#     state badge.
#   - ``RehearsalListResponse`` — paginated envelope; same shape as the
#     ``CustomScenarioListResponse`` (items / total / limit / offset /
#     has_more) so the picker UI can swap the source endpoint without
#     reshaping the store.
# Modified: 2026-05-26 (feat/foresight-v10-scenario-editor-backend) — RFC
# 08 v1.0 wave 3 adds the workspace-scoped custom-scenario surface:
#   - ``CustomScenarioParsedMetaDto`` — denormalized parse result on the
#     wire (num_personas / num_ticks / tier_mix / precedent_seed).
#   - ``CustomScenarioListItem`` + ``CustomScenarioListResponse`` —
#     ``GET /api/v1/foresight/scenarios/custom`` paginated envelope.
#   - ``CustomScenarioResponse`` —
#     ``GET /api/v1/foresight/scenarios/custom/{id}`` plus the POST/PUT
#     return shape (carries the full ``yaml_body``).
#   - ``CreateCustomScenarioRequest`` — POST/PUT body (full replace on
#     PUT; ``parsed_meta_override`` is accepted but ignored on the
#     write path — the service always recomputes the parsed meta from
#     the YAML body so the doc never drifts from its source-of-truth).
#   - ``CreateScenarioRequest.custom_scenario_id`` — optional field on
#     the run-create body that points at a workspace scenario; when
#     present, ``personas`` may be omitted and the server loads the
#     scenario's persona list from the saved YAML. ``sub_type`` /
#     ``n_ticks`` likewise default to the saved values when the field
#     is set, unless overridden on the request.
# Modified: 2026-05-26 (feat/foresight-v10-threshold-override-cloud) —
# RFC 08 v1.0 PR 10 adds the per-workspace threshold-override surface:
#     - ``ForesightThresholdResponse`` — shape returned by both GET and
#       PUT /api/v1/foresight/workspace/threshold. Carries the resolved
#       view (current / default / is_overridden / updated_at) the
#       paw-enterprise settings panel renders.
#     - ``SetForesightThresholdRequest`` — PUT body. Single-field shape
#       (``threshold: float | None``); float ∈ [0.5, 0.95] sets the
#       override, ``None`` resets to the default. Bounds are DTO-level
#       so a 422 fires before service code runs.
#   Contract is locked against Team A2's settings panel; the response
#   field names mirror the TypeScript surface that ships alongside.
# Modified: 2026-05-26 (feat/foresight-v10-live-snapshot-and-fixes) —
# RFC 08 v1.0 PR — three additions:
#   1. ``LiveSnapshotResponse`` + nested ``TierMixActual`` / ``SampledTrace``
#      / ``Anomaly`` DTOs — backing GET /runs/{id}/live-snapshot for the
#      paw-enterprise LivePanel UI. Contract is locked against PR #267.
#   2. ``GateDecision`` Pydantic sub-model — replaces the loose
#      ``dict[str, Any] | None`` on ``BacktestRunResponse.gate_decision``
#      and ``BacktestRunListItemResponse.gate_decision``. Mirrors the
#      :class:`ee.foresight.aggregator.ThresholdDecision.as_wire_dict()`
#      shape (passed / observed / threshold / margin / n_pairs) plus a
#      derived ``reason`` and an ``evaluated_at`` ISO-8601 timestamp.
#      Backward-compat — Pydantic's ``model_dump()`` produces the same
#      dict callers already consume.
#   3. ``CreateScenarioRequest`` gains optional ``precedent_seed`` and
#      ``precedent_seeds`` fields — the cloud body now mirrors the
#      engine YAML grammar (RFC §14.4). The engine's
#      ``NoOpDecisionGraphRef`` is seeded from these on the cloud-side
#      ``_run_engine_inline`` so ``ProjectedDecision.forward_precedent_decision_id``
#      gets a synthetic, deterministic id whenever the operator opts in.
# Modified: 2026-05-25 (feat/foresight-v15-scenarios-aggregate-insights) —
# RFC 08 §11.2 / §11.5 / §11.6 backing shapes:
#   - ``ScenarioCatalogItem`` + ``ScenarioCatalogResponse`` —
#     ``GET /api/v1/foresight/scenarios`` template enumeration.
#   - ``RollingAccuracyPointDto`` + ``RollingAccuracySeriesDto`` +
#     ``ConfidenceDriftDto`` + ``ModalOutcomeEntryDto`` +
#     ``ModalOutcomeDistributionDto`` + ``AggregateRollupResponse`` —
#     ``GET /api/v1/foresight/aggregate?window_days=N`` rollup output.
#   - ``InsightResponse`` + ``InsightsResponse`` —
#     ``GET /api/v1/foresight/insights`` synthesizer output.
#   The UI lead's TypeScript shapes mirror these field-for-field;
#   property names are locked to the contract in the §11 brief.
# Modified: 2026-05-25 (feat/foresight-v05-subtypes-projected-decision) — PR 5
#   adds the per-anchor projection fanout surface:
#     - ``ProjectedDecisionResponse`` — one record on the wire.
#     - ``ProjectedDecisionListResponse`` — paginated envelope for
#       ``GET /api/v1/foresight/runs/{id}/projected-decisions`` with the
#       ``total / limit / offset / has_more`` fields a paginating
#       client needs. v0.5 keeps the cursor offset-based; v1.0 may
#       swap to opaque cursors once the dataset grows past the point
#       where ``count_documents`` is cheap.
# Modified: 2026-05-25 (feat/foresight-v04-backtest-aggregator) — PR 4
#   adds the retroactive backtest gate surface:
#     - ``CreateBacktestRequest`` — POST /foresight/backtests body.
#     - ``BacktestRunResponse`` — POST + GET response.
#     - ``BacktestRunListItemResponse`` — lighter list shape.
#     - ``OnboardingGateResponse`` — GET /foresight/onboarding/gate.
#   Each is a distinct shape per the cloud rule #4 separation; the
#   request body is forbidding-extra so a typo at the operator side
#   surfaces as a 422 instead of a silent default.
# Modified: 2026-05-25 (feat/foresight-v07-cloud-mount) — PR 7 adds
#   ScenarioRunListItemResponse (lighter shape for GET /runs without
#   the inline ``result`` blob) and re-exports the existing v0.1 shapes
#   unchanged so any v0.1 caller keeps working.
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Request / response models for the Foresight REST surface. Per the
# ee/cloud rule #4 (DTOs separate request and response), every
# operation has its own *Request and *Response shape — even though
# v0.1 only ships two endpoints (POST /scenarios, GET /runs/:id),
# both have distinct request/response contracts that v1.0 will
# extend without breaking compatibility.

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PersonaSpecRequest(BaseModel):
    """One persona declared inline in a POST /scenarios body.

    The shape mirrors ``foresight.scenarios.runner.PersonaSpec`` but is
    a Pydantic model so FastAPI's request parser handles validation.
    v1.0 adds a soul_path field for soul-file-anchored personas
    (RFC §16.2 — synthesized souls in did:soul:synthesized:* namespace).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    role: str = Field(default="participant", max_length=64)
    ocean: dict[str, float] = Field(default_factory=dict)


class CreateScenarioRequest(BaseModel):
    """POST /api/v1/foresight/scenarios body.

    v0.1 accepts the inline scenario shape only (declarative personas
    in the body). v1.0 adds:
      - ``scenario_path``: load a YAML by path (for saved scenarios)
      - ``scenario_id``: reference a stored scenario by id
      - ``tier_mix_override``, ``budget_cap_usd``, ``activation_overlay``
        and the rest of RFC §18's grammar.

    PR 8 (RFC 08 §8) adds ``route_to_instinct``: when true, every
    ``ProjectedDecision`` the run emits also lands one row in the
    Instinct approval queue so the operator's Tray surfaces the
    forecast as evidence next to the matching real-world decision.
    Defaults to ``False`` so backwards-compatible callers (smoke
    runs, backtests, the chat-driven CLI) don't accidentally fan
    proposals into the Tray. The flag is documented on the scenario
    YAML files (``decision_forecast.yaml`` / ``market_sim.yaml`` /
    ``org_change.yaml``) as a v1.0 loader hook — v0.5 reads it only
    from the request body.

    v1.0 PR (this file) adds ``precedent_seed`` + ``precedent_seeds``
    — the cloud body now exposes the same forward-precedent grammar
    the engine YAML carries (RFC §14.4). When a seed is supplied the
    cloud's per-tick projection closure feeds it into the
    :class:`pocketpaw_ee.foresight.decision_graph_ref.NoOpDecisionGraphRef`
    so every persisted ``ForesightProjectedDecision`` doc gets a
    synthetic, deterministic ``forward_precedent_decision_id`` of the
    form ``synthetic-precedent-<sha1[:12]>``. ``None`` (the default)
    preserves the v0.5 "always None" wire shape for un-seeded
    scenarios. RFC §14.4 documents the synthetic-id semantics +
    backfill path when the real Decision Graph (RFC 07) lands.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    sub_type: str = Field(default="decision_forecast", max_length=64)
    n_ticks: int = Field(default=1, ge=1, le=1000)
    personas: list[PersonaSpecRequest] = Field(default_factory=list, max_length=1000)
    custom_scenario_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Optional workspace-scoped custom scenario id (RFC 08 v1.0 "
            "wave 3). When provided, the server loads the scenario's "
            "saved YAML body and uses it for the run instead of looking "
            "up by ``sub_type``. ``custom_scenario_id`` wins over the "
            "request's ``sub_type`` / ``personas`` / ``n_ticks`` when "
            "the YAML carries those fields. 422 "
            "(``foresight.custom_scenario_not_found``) if the id is "
            "unknown or cross-tenant. When the field is None (the "
            "default), the v0.5 inline-personas path runs unchanged."
        ),
    )
    route_to_instinct: bool = Field(
        default=False,
        description=(
            "When true, every ProjectedDecision the run emits is also "
            "fanned into the Instinct approval queue (RFC 08 §8). The "
            "proposal is EVIDENCE-only — approving it acknowledges the "
            "forecast but does NOT trigger an executing side-effect. "
            "Backtests cannot opt in (the backtest endpoint reuses the "
            "scenario runner but disables this fan-out)."
        ),
    )
    precedent_seed: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Optional global forward-precedent seed (RFC 08 §14.4). When "
            "set, every persisted ProjectedDecision for the run gets a "
            "synthetic, deterministic ``forward_precedent_decision_id`` "
            "derived from sha1(scenario_id|anchor_id|persona_id|seed). "
            "Same inputs always produce the same id. ``None`` keeps the "
            "v0.5 wire shape (every projection's precedent id is "
            "``None``). The engine's YAML scenarios carry this field at "
            "the scenario root; v1.0 lifts it onto the cloud body so "
            "operators can drive synthetic-precedent runs from the API."
        ),
    )
    precedent_seeds: dict[str, str] | None = Field(
        default=None,
        description=(
            "Per-anchor precedent seed overrides (RFC 08 §14.4). Keys "
            "are anchor ids (e.g. ``decision:renewal``, "
            "``segment:enterprise``, ``rollout:training``); values are "
            "the seeds to use for that anchor. An anchor-level override "
            "wins over the scenario-wide ``precedent_seed``. Pass "
            "``None`` (or omit) to apply the global seed uniformly."
        ),
    )


class ScenarioRunResponse(BaseModel):
    """POST /scenarios response + GET /runs/:id response.

    v0.1 returns a single shape for both endpoints (immediately-completed
    run on POST; same shape on GET). v1.0 will split these — POST
    returns a "queued" envelope with the run id and a websocket subscription
    URL, GET returns the full result with the per-tick aggregates and
    projected decisions stream.

    PR 7 keeps the v0.1 wire field set (id, scenario_name, status,
    created_at, request, result, error) and adds an optional
    ``workspace_id`` so the cloud surface can echo the tenancy key the
    persistence layer enforces. Older callers that only consumed the
    v0.1 fields keep working — Pydantic's default ``extra="forbid"``
    constraint is unchanged at the request side; responses tolerate
    additional fields client-side.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str | None = None
    scenario_name: str
    status: str  # "queued" | "running" | "complete" | "failed"
    created_at: str  # ISO-8601
    updated_at: str | None = None
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class ScenarioRunListItemResponse(BaseModel):
    """Lighter shape for ``GET /runs`` — drops the inline ``result`` and
    ``request`` blobs so the list endpoint stays cheap on workspaces
    that have accumulated dozens of runs.

    The detail endpoint (``GET /runs/{id}``) returns the full
    :class:`ScenarioRunResponse` shape; the frontend Scenarios + Live
    panels (RFC §11.2 / §11.3) use the list shape for cards and call
    the detail endpoint when the operator clicks through.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str | None = None
    scenario_name: str
    status: str
    created_at: str
    updated_at: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Backtest gate (RFC §10 + §13.1 gate 7) — retroactive runs scored against
# ground truth; the unlock criterion for forward sims.
# ---------------------------------------------------------------------------


class HistoricalAnchorRequest(BaseModel):
    """One historical-decision anchor for a backtest run.

    v0.1 keeps this minimal: the anchor object id (Fabric ``kind:id``),
    the known actual outcome dict (so the aggregator can pair against
    it without an out-of-band lookup), and an optional ``observed_at``
    so listeners can compute time-bucketed accuracy. v1.0 will pull
    anchors from the Fabric/journal connector directly and the request
    shape will collapse to a query window.
    """

    model_config = ConfigDict(extra="forbid")

    anchor_object_id: str = Field(..., min_length=1, max_length=256)
    actual_outcome: dict[str, Any] = Field(default_factory=dict)
    scenario_template: str = Field(default="decision_forecast.yaml", max_length=128)
    projection_confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CreateBacktestRequest(BaseModel):
    """POST /api/v1/foresight/backtests body.

    Reuses the forward-run grammar for personas + sub_type + n_ticks so
    operators don't learn a second vocabulary; adds:

    - ``anchors``: the historical decisions the backtest scores against.
      One pair per anchor. v0.1 takes the actual_outcome inline; v1.0
      will accept a Fabric query window instead.
    - ``threshold``: optional per-run threshold override (defaults to the
      workspace's effective threshold). Capped at [0.0, 1.0]; the gate
      can only be tightened, not relaxed below the default — that's
      enforced in the service layer so the DTO stays a plain shape.

    The response shape (:class:`BacktestRunResponse`) carries both the
    raw run result and the gate decision so the UI can render the
    unlock label without a second round trip.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    sub_type: str = Field(default="decision_forecast", max_length=64)
    n_ticks: int = Field(default=1, ge=1, le=1000)
    personas: list[PersonaSpecRequest] = Field(..., min_length=1, max_length=1000)
    anchors: list[HistoricalAnchorRequest] = Field(..., min_length=1, max_length=500)
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class GateDecision(BaseModel):
    """The structured wire shape for a backtest's gate verdict.

    v0.5 stored this as a free-form ``dict[str, Any] | None`` so a typo
    on the write path could ship to the UI without anyone noticing.
    v1.0 tightens the wire surface to this Pydantic model — mirrors the
    :class:`ee.foresight.aggregator.ThresholdDecision.as_wire_dict()`
    payload (``passed`` / ``observed`` / ``threshold`` / ``margin`` /
    ``n_pairs``) plus two derived fields the UI lead requested:

    - ``reason``: short label the Aggregate / Onboarding panels render
      next to the pass/fail badge. Vocabulary:
        * ``no_pairs`` — ``n_pairs == 0`` (gate never ran a comparison)
        * ``threshold_met`` — ``passed=True``
        * ``threshold_unmet`` — ``passed=False`` and ``n_pairs >= 1``
        * fallback — free-form string for future error states
    - ``evaluated_at``: ISO-8601 UTC timestamp the gate was scored.
      Captured at backtest completion; surfaces on the wire so the UI
      can render "scored 2 hours ago" without a second round trip.

    Field-name fidelity: ``observed`` is the raw modal accuracy the
    aggregator reports (kept on the wire so existing callers that key
    on ``gate_decision["observed"]`` keep working). ``modal_accuracy``
    is an alias-style mirror so the UI lead's TypeScript shape doesn't
    have to learn the aggregator's internal vocabulary; the service
    populates both with the same float so they never diverge.

    Backward-compat: existing callers that read ``gate_decision`` as a
    dict still work — Pydantic's :meth:`model_dump` produces a dict
    with the legacy keys (``passed`` / ``observed`` / ``threshold`` /
    ``margin`` / ``n_pairs``) plus the new ones, so a ``dict.get(key)``
    against the dump returns the same value v0.5 did.
    """

    model_config = ConfigDict(extra="forbid")

    passed: bool
    threshold: float = Field(..., ge=0.0, le=1.0)
    observed: float = Field(..., ge=0.0, le=1.0)
    modal_accuracy: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "UI-friendly alias of ``observed``. The service populates "
            "both with the same float so the two never diverge. "
            "Optional on the wire so a future write path that omits the "
            "alias still validates."
        ),
    )
    margin: float = Field(
        default=0.0,
        description=(
            "Signed accuracy margin (``observed - threshold``). Negative when the gate failed."
        ),
    )
    n_pairs: int = Field(..., ge=0)
    reason: str = Field(
        default="threshold_unmet",
        max_length=64,
        description=(
            "Short label the UI renders next to the pass/fail badge. "
            "v1.0 vocabulary: ``no_pairs`` | ``threshold_met`` | "
            "``threshold_unmet``. Free-form fallback allowed so a "
            "future error path can surface a custom string without a "
            "DTO migration."
        ),
    )
    evaluated_at: str = Field(
        ...,
        description="ISO-8601 UTC timestamp the gate was scored.",
    )


class BacktestRunResponse(BaseModel):
    """POST /backtests response + GET /backtests/:id response.

    Mirrors :class:`ScenarioRunResponse` plus two backtest-specific
    fields:

    - ``gate_decision``: a :class:`GateDecision` sub-model once the
      backtest completes (``None`` while queued / running / failed).
      The UI's Aggregate panel reads this directly to render the unlock
      label without re-computing. v0.5 typed this as a free-form
      ``dict[str, Any] | None`` — v1.0 tightens to the structured
      sub-model. Pydantic's ``model_dump()`` produces the same dict
      callers already consume, so the change is backward-compatible.
    - ``threshold``: the gate threshold this backtest was scored against
      (echoed back so the operator can reconcile the verdict with the
      bar it was measured against, even if the workspace default has
      since been tuned).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str | None = None
    scenario_name: str
    status: str  # "queued" | "running" | "complete" | "failed"
    created_at: str  # ISO-8601
    updated_at: str | None = None
    request: dict[str, Any]
    threshold: float
    result: dict[str, Any] | None = None
    gate_decision: GateDecision | None = None
    error: str | None = None


class BacktestRunListItemResponse(BaseModel):
    """Lighter shape for ``GET /backtests`` — drops the inline
    ``result`` / ``request`` blobs but keeps ``gate_decision`` so the
    list can render the unlock label per row without a click-through.

    ``gate_decision`` is the structured :class:`GateDecision` sub-model
    so the list shape matches the detail shape. v0.5 typed this loosely
    as ``dict[str, Any] | None``; v1.0 follows the detail endpoint's
    tightening (same backward-compat reasoning)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str | None = None
    scenario_name: str
    status: str
    created_at: str
    updated_at: str | None = None
    threshold: float
    gate_decision: GateDecision | None = None
    error: str | None = None


class OnboardingGateResponse(BaseModel):
    """GET /api/v1/foresight/onboarding/gate response.

    Derived from the latest completed backtest in the workspace. The
    UI's onboarding flow polls this on the new-workspace path; the
    Scenarios panel checks ``unlocked`` before letting the operator
    start a forward sim.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    unlocked: bool
    threshold: float
    reason: str  # "no_backtest" | "below_threshold" | "in_flight" | "unlocked"
    last_backtest_id: str | None = None
    last_backtest_accuracy: float | None = None
    last_backtest_at: str | None = None


# ---------------------------------------------------------------------------
# ProjectedDecision (RFC §7.7) — PR 5 per-anchor projection fanout.
# ---------------------------------------------------------------------------


class ProjectedDecisionResponse(BaseModel):
    """One projected-decision record on the wire.

    Mirrors :class:`pocketpaw_ee.cloud.foresight.domain.ProjectedDecision`
    plus the ISO-8601 ``created_at`` string. The list endpoint
    (``GET /runs/{id}/projected-decisions``) returns these in
    ``(tick_id, anchor_id)`` order — bounded by the index on the
    persistence layer.

    ``forward_precedent_decision_id`` is reserved for the RFC 07
    Decision Graph backfill path; v0.5 always reports ``None`` because
    RFC 07 isn't yet integrated into pocketpaw. The field is on the
    response so frontend consumers can render the link as soon as the
    backfill pass starts populating it without a wire-shape bump.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str
    run_id: str
    anchor_id: str
    persona_id: str
    tick_id: int
    decision_text: str
    confidence: float
    sub_type: str
    forward_precedent_decision_id: str | None = None
    created_at: str | None = None


class ProjectedDecisionListResponse(BaseModel):
    """Paginated wrapper for ``GET /runs/{id}/projected-decisions``.

    PR 5 returns a flat envelope with the items and the cursor metadata
    a paginating client needs: ``total`` (when cheap to compute under
    the workspace + run filter), ``limit``, ``offset``, and a
    ``has_more`` boolean derived from
    ``offset + len(items) < total``. The frontend Live panel uses the
    items array; cost-aware consumers (the v1.0 export endpoint) read
    the totals to size their fetch.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[ProjectedDecisionResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    has_more: bool = False


# ---------------------------------------------------------------------------
# Foresight → Instinct approval-loop fan-out (RFC 08 §8 + PR 8).
#
# When a scenario opts in via ``route_to_instinct=True``, each
# ProjectedDecision becomes one row in the Instinct approval queue.
# These response shapes wrap the persisted Instinct Action rows back
# into a Foresight-flavoured view so the Tray UI can render
# "the proposals spawned by THIS run" without poking the generic
# ``/instinct/actions/pending`` endpoint with a client-side filter.
# ---------------------------------------------------------------------------


class ForesightInstinctProposalResponse(BaseModel):
    """One Instinct proposal spawned by a Foresight ProjectedDecision.

    A subset of the full ``Action`` shape — enough for the Tray rail's
    Foresight column to render the row without requesting the
    Instinct detail endpoint. Operators who need the full Action
    payload (corrections, audit) fetch it via
    ``GET /api/v1/instinct/actions/{id}`` keyed by ``action_id``.

    Fields mirror the Instinct ``Action`` model where they apply,
    plus the ``foresight`` provenance block the bridge stamped onto
    ``parameters._foresight`` at propose time so the consumer can
    rehydrate the originating (run × tick × anchor) without a second
    round trip.
    """

    model_config = ConfigDict(extra="forbid")

    action_id: str
    pocket_id: str
    title: str
    description: str
    recommendation: str
    status: str  # "pending" | "approved" | "rejected" | "executed" | "failed"
    priority: str  # "low" | "medium" | "high" | "critical"
    category: str  # "data" for foresight evidence proposals
    assignee: str | None = None
    created_at: str | None = None
    # Provenance — the ``_foresight`` block the bridge stamped on
    # ``parameters`` at propose time. Carrying it on the response lets
    # the Tray UI render the "Why?" drawer (originating run / tick /
    # anchor / confidence) without a second API call.
    foresight: dict[str, Any]


class ForesightInstinctProposalListResponse(BaseModel):
    """Paginated wrapper for
    ``GET /runs/{id}/instinct-proposals``.

    Mirrors :class:`ProjectedDecisionListResponse`: the items array
    plus the cursor metadata a paginating client needs. v0.8 keeps
    the cursor offset-based for parity with the projection-list
    endpoint; v1.0 may swap to opaque cursors once dataset sizes
    make a count_documents call expensive.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[ForesightInstinctProposalResponse]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    has_more: bool = False


# ---------------------------------------------------------------------------
# Scenario catalog (RFC §11.2) — ``GET /api/v1/foresight/scenarios``.
#
# Static enumeration of the bundled YAML scenario templates. The UI's
# Scenarios panel reads this to populate the "Run a scenario" picker;
# the response is small (one row per template) and changes only on
# code releases, so the loader caches it at module import.
# ---------------------------------------------------------------------------


class ScenarioCatalogItem(BaseModel):
    """One scenario template entry surfaced in the catalog.

    Fields mirror the §11.2 contract: ``id`` is the YAML stem (also
    the sub_type for the three v0.5-shipped templates); ``name`` is
    the human label; ``description`` is a short blurb the UI renders
    next to the card; ``num_personas`` and ``num_ticks`` give the
    operator a feel for the scenario shape before they run it;
    ``tier_mix`` echoes the locked default so the cost-aware operator
    can see the L2 backend split without expanding the row.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    sub_type: str
    description: str
    num_personas: int = Field(ge=0)
    num_ticks: int = Field(ge=0)
    tier_mix: dict[str, float]


class ScenarioCatalogResponse(BaseModel):
    """``GET /api/v1/foresight/scenarios`` response.

    Flat envelope — no pagination because the catalog ships exactly
    three templates in v0.5; v1.0 may grow this once the remaining
    four RFC §4 sub-types land. The order matches the YAML on-disk
    sort so the picker renders deterministically across deploys.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[ScenarioCatalogItem]


# ---------------------------------------------------------------------------
# Aggregate rollup (RFC §11.5) — ``GET /api/v1/foresight/aggregate``.
#
# Rolling time-windowed view of accuracy, confidence drift, and modal
# outcome distribution across the workspace's recent backtests +
# scenario runs. ``window_days`` query parameter controls the look-back
# window; defaults to 30, capped at 90 (422 above) per the §11.5
# contract.
# ---------------------------------------------------------------------------


class RollingAccuracyPointDto(BaseModel):
    """One time-bucketed accuracy reading.

    ``ts`` is the bucket-end timestamp (ISO-8601 UTC); ``accuracy`` is
    the modal accuracy across the bucket; ``sample_count`` is the
    number of pairs (or proxy records) that fed the bucket so the UI
    can show "thin sample" warnings without a second round trip.
    """

    model_config = ConfigDict(extra="forbid")

    ts: str
    accuracy: float = Field(ge=0.0, le=1.0)
    sample_count: int = Field(ge=0)


class RollingAccuracySeriesDto(BaseModel):
    """Series wrapper for ``rolling_accuracy.points``."""

    model_config = ConfigDict(extra="forbid")

    points: list[RollingAccuracyPointDto] = Field(default_factory=list)


class ConfidenceDriftDto(BaseModel):
    """Confidence-drift summary across the window.

    ``trend`` is the bucket label the synthesizer reads; ``magnitude``
    is the absolute drift size. The aggregator emits ``rising``,
    ``falling``, or ``flat`` based on a configurable flat-threshold.
    """

    model_config = ConfigDict(extra="forbid")

    trend: str  # "rising" | "falling" | "flat"
    magnitude: float = Field(ge=0.0)


class ModalOutcomeEntryDto(BaseModel):
    """One row in the modal-outcome distribution.

    ``outcome`` is the string value (e.g. ``"approved"``,
    ``"rejected"``); ``share`` is the fraction of pairs that landed
    that value across the window. Shares across the entries are
    normalized to sum to 1.0 (within floating-point rounding).
    """

    model_config = ConfigDict(extra="forbid")

    outcome: str
    share: float = Field(ge=0.0, le=1.0)


class ModalOutcomeDistributionDto(BaseModel):
    """Distribution wrapper for the modal-outcome rollup."""

    model_config = ConfigDict(extra="forbid")

    entries: list[ModalOutcomeEntryDto] = Field(default_factory=list)


class AggregateRollupResponse(BaseModel):
    """``GET /api/v1/foresight/aggregate?window_days=N`` response.

    Read-only — derived from the workspace's persisted backtests +
    projected-decision records over the window. Empty workspaces
    return zeros + empty arrays (never 404) so the UI's Aggregate
    panel can render the empty state without a separate code path.
    """

    model_config = ConfigDict(extra="forbid")

    window_days: int = Field(ge=1, le=90)
    generated_at: str
    rolling_accuracy: RollingAccuracySeriesDto
    confidence_drift: ConfidenceDriftDto
    modal_outcome_distribution: ModalOutcomeDistributionDto


# ---------------------------------------------------------------------------
# Insights (RFC §11.6) — ``GET /api/v1/foresight/insights``.
#
# Pattern-based synthesizer output — the v0.1 rules live in
# ``ee.foresight.insights`` (pure module, no I/O). v1.0 will swap the
# rule engine for an LLM synthesizer; the wire shape stays.
# ---------------------------------------------------------------------------


class InsightResponse(BaseModel):
    """One synthesized insight row.

    Mirrors :class:`pocketpaw_ee.foresight.insights.Insight` plus the
    ISO-8601 ``generated_at`` string. ``anchor_refs`` is a list of
    optional link targets the UI renders as inline pills (e.g.
    ``anchor:rollout:training``, ``persona:enterprise-acme``,
    ``backtest:5f5...``).

    ``severity`` vocabulary is locked to ``info | warning | critical``
    so the frontend can map each level to a stable colour without
    consulting a dictionary.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str  # "accuracy_drop" | "persona_outlier" | "tier_imbalance"
    # | "trend_break" | "threshold_unmet"
    title: str
    body: str
    severity: str  # "info" | "warning" | "critical"
    anchor_refs: list[str] = Field(default_factory=list)
    generated_at: str


class InsightsResponse(BaseModel):
    """``GET /api/v1/foresight/insights`` response.

    Flat envelope; the synthesizer caps at 20 items by default
    (pagination lands in v1.0 once the LLM synthesizer can fan
    finer-grained rules). Items are sorted by severity descending
    (critical > warning > info) then ``generated_at`` descending.

    ``synth_source`` reports which synthesizer ACTUALLY produced the
    rows the caller is reading:

      - ``"pattern"`` (default) — the deterministic v0.5 five-rule
        synthesizer ran. This is the default for any workspace that
        hasn't opted into the LLM synth, AND for the fallback path
        when an LLM run returned empty / failed.
      - ``"llm"`` — the v1.0 LLM synthesizer produced the rows.

    Dashboard + chat agent surfaces this so users can tell whether
    they're reading deterministic rule output or an LLM narrative.
    Defaulting to ``"pattern"`` keeps existing callers / fixtures
    that construct ``InsightsResponse`` without the field working.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[InsightResponse]
    synth_source: Literal["pattern", "llm"] = "pattern"


# ---------------------------------------------------------------------------
# Live snapshot (RFC 08 §11.3) — GET /api/v1/foresight/runs/{id}/live-snapshot.
#
# Compact "what's happening right now" view of a single Foresight run.
# Backs the paw-enterprise LivePanel. Contract is locked against PR
# #267 (paw-enterprise) — every field name + nesting shape below
# mirrors the TypeScript shape the UI lead built against.
# ---------------------------------------------------------------------------


class TierMixActual(BaseModel):
    """Actual tier mix observed across the run's personas.

    Each field is the share of personas assigned to that tier; the
    three values should sum to ~1.0 (rounding allowed). When the run
    hasn't fanned any projections yet (or the engine pool is empty),
    the service returns zeros across all three — callers should not
    expect a strict sum-to-1 invariant on an empty run.
    """

    model_config = ConfigDict(extra="forbid")

    premium: float = Field(default=0.0, ge=0.0, le=1.0)
    mid: float = Field(default=0.0, ge=0.0, le=1.0)
    tail: float = Field(default=0.0, ge=0.0, le=1.0)


class SampledTrace(BaseModel):
    """One sampled per-tick projection trace for the LivePanel timeline.

    The service samples up to 10 :class:`ProjectedDecisionResponse`
    rows per run (deterministically, by tick id ascending) so the
    panel renders a stable slice across re-fetches without paginating.
    ``action_summary`` is built by a sub-type-aware formatter (mirrors
    the Instinct bridge labelling so the operator sees consistent
    text across the Tray + LivePanel) and capped at 200 chars.
    """

    model_config = ConfigDict(extra="forbid")

    tick_id: int = Field(..., ge=0)
    persona_id: str = Field(default="", max_length=128)
    sub_type: str = Field(..., max_length=64)
    action_summary: str = Field(..., max_length=200)
    confidence: float = Field(..., ge=0.0, le=1.0)


class Anomaly(BaseModel):
    """One anomaly flagged on the run snapshot.

    Three rule kinds ship in v1.0 (see
    :mod:`pocketpaw_ee.cloud.foresight.live_snapshot` for the
    detector implementations):

    - ``tier_drift`` — actual tier mix deviates from configured 5/15/80
      by more than 0.15 (info) or 0.25 (warning).
    - ``confidence_spike`` — confidence distribution skews extreme
      (variance < 0.02 with mean > 0.8 OR mean < 0.2) → info; low-mean
      with sample count ≥ 5 → warning.
    - ``stalled_persona`` — any persona's last decision ts > 30s
      behind the run's latest tick ts (warning); zero decisions while
      the run reached >0 ticks (critical).

    ``severity`` vocabulary mirrors the §11.6 insights surface so the
    UI's severity → colour mapping is uniform across panels.
    ``body`` is capped at 240 chars (the LivePanel renders these as
    one-line pills).
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., max_length=64)
    severity: str = Field(..., max_length=16)
    body: str = Field(..., max_length=240)


class LiveSnapshotResponse(BaseModel):
    """``GET /api/v1/foresight/runs/{id}/live-snapshot`` response.

    Compact, read-only view of the run "as it stands right now".
    Workspace-scoped — an unknown / cross-tenant run id collapses to a
    404 (existence not leakable). Empty runs return zeros + empty
    arrays — the UI never sees a 404 on a fresh run.

    Contract is locked to paw-enterprise PR #267; any breaking change
    requires the UI repo's contract test to flip first.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    generated_at: str
    status: str  # "created" | "running" | "complete" | "failed"
    tier_mix_actual: TierMixActual
    sampled_traces: list[SampledTrace] = Field(default_factory=list, max_length=10)
    anomalies: list[Anomaly] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-workspace onboarding-gate threshold override (RFC 08 v1.0 PR 10).
#
# Contract locked to paw-enterprise Team A2's settings panel — the
# admin's "Foresight" preferences row reads the GET response to render
# the current vs. default values, and the PUT body is the only mutation
# surface the UI exposes (no inline edit on the Onboarding panel itself).
# ---------------------------------------------------------------------------


class ForesightThresholdResponse(BaseModel):
    """Response shape for both
    ``GET /api/v1/foresight/workspace/threshold`` and
    ``PUT /api/v1/foresight/workspace/threshold``.

    Carries the resolved view the UI renders:

    - ``current_threshold``: the effective threshold the gate / backtest
      scorer apply right now. When ``is_overridden=False`` this equals
      ``default_threshold``; when ``is_overridden=True`` it equals the
      admin-set override.
    - ``default_threshold``: the global default
      (``GATE_DEFAULT_THRESHOLD = 0.65``). Echoed back so the UI can
      render "default 0.65" next to the override input without a second
      round trip or a hard-coded constant on the frontend.
    - ``is_overridden``: ``True`` when a per-workspace
      :class:`pocketpaw_ee.cloud.models.foresight_workspace_config.ForesightWorkspaceConfig`
      doc carries a non-null ``threshold_override``. ``False`` when no
      doc exists or its override is ``None``.
    - ``updated_at``: ISO-8601 UTC timestamp the override was last
      written. ``None`` when ``is_overridden=False`` (no override → no
      meaningful timestamp).
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    current_threshold: float = Field(..., ge=0.5, le=0.95)
    default_threshold: float = Field(..., ge=0.5, le=0.95)
    is_overridden: bool
    updated_at: str | None = None


class SetForesightThresholdRequest(BaseModel):
    """``PUT /api/v1/foresight/workspace/threshold`` body.

    Single-field shape:

    - ``threshold: float | None`` — a float in the closed range
      ``[0.5, 0.95]`` sets the workspace override; ``None`` resets the
      workspace to the global default (deletes the override).

    Bounds chosen for the captain-approved override window:
      - Lower bound 0.5 keeps operators from relaxing the gate to a
        meaningless level (random guessing on binary outcomes).
      - Upper bound 0.95 keeps operators from setting an unreachable
        bar (the v0.1 deterministic engine + a 10-anchor backtest can
        cap below 1.0 due to mismatch noise).

    The service emits ``foresight.threshold.updated`` whenever the
    effective override changes (a no-op write that keeps the same value
    does NOT emit — the UI's optimistic local state shouldn't get
    rebroadcast for free).
    """

    model_config = ConfigDict(extra="forbid")

    threshold: float | None = Field(default=None, ge=0.5, le=0.95)


# ---------------------------------------------------------------------------
# Per-workspace insights-synthesizer config (RFC 08 v1.0 — LLM insights PR).
#
# Companion endpoint to the threshold override above. Same shape pattern
# (separate request / response with explicit field bounds) so the
# settings panel can read + write each knob independently. The synthesizer
# choice defaults to "pattern" — the v0.5 deterministic synthesizer
# stays as the default. Workspaces opt into "llm" explicitly via the
# PUT body.
# ---------------------------------------------------------------------------


class ForesightInsightsConfigResponse(BaseModel):
    """Response shape for both
    ``GET /api/v1/foresight/workspace/insights-config`` and
    ``PUT /api/v1/foresight/workspace/insights-config``.

    Fields:
      - ``workspace_id``: tenancy key (echoed for client-side
        bookkeeping).
      - ``synthesizer``: ``"pattern"`` (default — the v0.5 deterministic
        five-rule synthesizer) or ``"llm"`` (the v1.0 LLM-driven
        synthesizer; opt-in only). LLM failures fall back to the
        pattern synthesizer so the wire response never 5xxs.
      - ``llm_cache_ttl_seconds``: the in-memory LRU TTL the LLM
        synthesizer applies to its per-workspace cache. v1.0 echoes the
        module constant (300 seconds) so the UI can render the cost-
        discipline note without round-tripping a separate read.
      - ``updated_at``: ISO-8601 UTC timestamp of the last config write.
        ``None`` when no config row exists for the workspace.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    synthesizer: Literal["pattern", "llm"]
    llm_cache_ttl_seconds: int = Field(..., ge=1)
    updated_at: str | None = None


class SetForesightInsightsConfigRequest(BaseModel):
    """``PUT /api/v1/foresight/workspace/insights-config`` body.

    Single-field shape:

    - ``synthesizer: Literal["pattern", "llm"]`` — the synthesizer the
      workspace's ``/insights`` endpoint runs by default. ``"pattern"``
      keeps the v0.5 deterministic five-rule synthesizer; ``"llm"`` opts
      into the LLM-driven synthesizer.

    Bounds rationale:
      - The Literal forbids the third-state ``None`` / unknown values so
        a typo (``"LLM"``, ``"ai"``) gets 422'd at the DTO layer rather
        than silently coerced.
      - The LLM path has a hard fallback to ``pattern`` on failure
        (timeouts, malformed JSON, etc.) so a workspace stuck on "llm"
        during an outage still gets pattern-rule insights.

    The service emits ``foresight.insights_config.updated`` whenever
    the effective synthesizer changes. A no-op write (same value)
    stays quiet so the UI's optimistic local state doesn't echo.
    """

    model_config = ConfigDict(extra="forbid")

    synthesizer: Literal["pattern", "llm"]


# ---------------------------------------------------------------------------
# Custom scenarios (Team 1 wave 3) — workspace-scoped scenario YAML
# storage + CRUD wire shapes (RFC 08 v1.0).
#
# Operators save their own scenario YAMLs against the workspace and
# point runs at them via ``CreateScenarioRequest.custom_scenario_id``.
# Three v1.0-supported ``sub_type`` values keep the surface aligned
# with the engine's ``SUPPORTED_SUB_TYPES`` tuple; broaden in lockstep
# when the engine adds more sub-types.
# ---------------------------------------------------------------------------


# The literal mirrors the engine's
# ``pocketpaw_ee.foresight.subtypes.SUPPORTED_SUB_TYPES`` set. Keeping it
# inline (rather than importing from the engine module) preserves the
# import-linter contract "cloud → engine forbidden"; updates here must
# stay in lockstep with the engine.
CustomScenarioSubType = Literal["decision_forecast", "market_sim", "org_change_rehearsal"]


class CustomScenarioParsedMetaDto(BaseModel):
    """Denormalized parse result for a custom scenario.

    Service stamps this onto the doc at write time so the list endpoint
    can render ``num_personas`` / ``num_ticks`` / ``tier_mix`` without
    re-parsing the YAML body on every read. ``precedent_seed`` is the
    scenario-root seed if present in the YAML, else ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    num_personas: int = Field(..., ge=0)
    num_ticks: int = Field(..., ge=0)
    tier_mix: dict[str, float] = Field(default_factory=dict)
    precedent_seed: str | None = None


class CustomScenarioListItem(BaseModel):
    """Lighter shape for the list endpoint — drops the inline ``yaml_body``
    blob so a workspace with dozens of saved scenarios serves the list
    cheaply. The detail endpoint (``GET /scenarios/custom/{id}``) returns
    the full :class:`CustomScenarioResponse`.

    ``num_personas`` and ``num_ticks`` are surfaced flat (rather than
    nested inside ``parsed_meta``) so the picker UI can sort / filter
    without unpacking the meta block.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    sub_type: CustomScenarioSubType
    description: str = ""
    author: str = ""
    num_personas: int = Field(..., ge=0)
    num_ticks: int = Field(..., ge=0)
    updated_at: str  # ISO-8601


class CustomScenarioListResponse(BaseModel):
    """``GET /api/v1/foresight/scenarios/custom`` paginated envelope.

    Pagination is offset-based for parity with the other foresight list
    endpoints (projected-decisions, instinct-proposals); ``limit`` is
    capped at 100 at the router layer. ``has_more`` is a derived
    boolean (``offset + len(items) < total``) so the picker UI doesn't
    have to compute it client-side.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[CustomScenarioListItem]
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=1, le=100)
    offset: int = Field(..., ge=0)
    has_more: bool


class CustomScenarioResponse(BaseModel):
    """``GET /scenarios/custom/{id}`` plus POST/PUT return shape.

    Carries the full ``yaml_body`` so the editor UI can re-populate its
    Monaco / Codemirror buffer with one round trip. ``parsed_meta`` is
    the denormalized parse result the service stamped at write time.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str
    name: str
    sub_type: CustomScenarioSubType
    description: str = ""
    author: str = ""
    created_at: str  # ISO-8601
    updated_at: str  # ISO-8601
    yaml_body: str
    parsed_meta: CustomScenarioParsedMetaDto


class CreateCustomScenarioRequest(BaseModel):
    """POST/PUT body for ``/api/v1/foresight/scenarios/custom``.

    ``parsed_meta_override`` is accepted but ignored on the write path
    — the service always recomputes parsed meta from the YAML body so
    the doc never drifts from its source of truth. The field is reserved
    for forward-compat (a future "save without re-parsing" flow) and to
    let the editor UI echo its locally-computed meta back without an
    extra round trip.

    Validation order (DTO → service):
      - DTO enforces field-shape bounds: ``name`` ≤120, ``description``
        ≤500, ``yaml_body`` ≤64 KB, ``sub_type`` in the engine's
        supported set.
      - Service parses the YAML; mismatches between the request
        ``sub_type`` and the YAML's ``sub_type`` surface as
        ``foresight.sub_type_mismatch`` (422). Persona / tick caps and
        the tier-mix sum constraint surface as
        ``foresight.invalid_scenario`` (422).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    sub_type: CustomScenarioSubType = Field(default="decision_forecast")
    description: str = Field(default="", max_length=500)
    yaml_body: str = Field(..., min_length=1, max_length=65536)
    parsed_meta_override: CustomScenarioParsedMetaDto | None = Field(
        default=None,
        description=(
            "Optional client-side parsed-meta echo. Service ignores this "
            "field on the write path — it always recomputes the parsed "
            "meta from the YAML body so the doc never drifts from its "
            "source of truth. Reserved for forward-compat."
        ),
    )


# ---------------------------------------------------------------------------
# Rehearsals (v2 landing) — joined view of custom scenarios + their latest
# run, used by the paw-enterprise ``/foresight`` landing's RehearsalCard so
# each card can render ``run_count`` + a "last run was X" badge without an
# N+1 client-side fetch per card.
#
# The shape is intentionally close to ``CustomScenarioListItem`` so the
# editor picker UI can keep its sorts / filters wired; the additions
# (``run_count`` / ``last_run``) are additive.
# ---------------------------------------------------------------------------


class RehearsalLastRun(BaseModel):
    """Compact summary of a rehearsal's most recent run.

    Embedded on each :class:`RehearsalListItem` so the v2 landing card can
    render the "last run was X" badge without a separate fetch per
    scenario. ``verdict_summary`` is a best-effort one-line string derived
    from ``ForesightRun.result`` (modal outcome when present, else a
    generic "Run complete"); it is intentionally short (≤120 chars) so
    the card layout stays predictable. ``None`` when the run is still
    in flight (status ``running``).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    status: Literal["queued", "running", "complete", "failed"]
    ran_at: str  # ISO-8601 UTC; sourced from the run doc's ``createdAt``.
    verdict_summary: str | None = Field(default=None, max_length=120)


class RehearsalListItem(BaseModel):
    """One row on the v2 ``/foresight`` landing.

    Mirrors :class:`CustomScenarioListItem` field set + two joined fields
    the landing card needs to render its state badge:

      - ``run_count`` — total number of runs ever spawned from this
        scenario in the workspace. ``0`` means the scenario is a draft.
      - ``last_run`` — compact summary of the most recent run (or
        ``None`` when ``run_count == 0``).

    Field-name fidelity with the sibling ``CustomScenarioListItem`` is
    deliberate: paw-enterprise's editor picker can swap source endpoints
    (``/scenarios/custom`` ↔ ``/rehearsals``) without reshaping the store
    — only the two new fields layer on.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    sub_type: CustomScenarioSubType
    description: str = ""
    num_personas: int = Field(..., ge=0)
    num_ticks: int = Field(..., ge=0)
    updated_at: str  # ISO-8601
    run_count: int = Field(default=0, ge=0)
    last_run: RehearsalLastRun | None = None


class RehearsalListResponse(BaseModel):
    """``GET /api/v1/foresight/rehearsals`` paginated envelope.

    Same pagination shape as :class:`CustomScenarioListResponse` (items /
    total / limit / offset / has_more) so the v2 landing's data hook can
    reuse the cursor logic. ``limit`` is capped at 100 at the router
    layer; the default 50 matches the v2 landing's first-paint quota.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[RehearsalListItem]
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=1, le=100)
    offset: int = Field(..., ge=0)
    has_more: bool


__all__ = [
    "AggregateRollupResponse",
    "Anomaly",
    "BacktestRunListItemResponse",
    "BacktestRunResponse",
    "ConfidenceDriftDto",
    "CreateBacktestRequest",
    "CreateCustomScenarioRequest",
    "CreateScenarioRequest",
    "CustomScenarioListItem",
    "CustomScenarioListResponse",
    "CustomScenarioParsedMetaDto",
    "CustomScenarioResponse",
    "CustomScenarioSubType",
    "ForesightInsightsConfigResponse",
    "ForesightInstinctProposalListResponse",
    "ForesightInstinctProposalResponse",
    "ForesightThresholdResponse",
    "GateDecision",
    "HistoricalAnchorRequest",
    "InsightResponse",
    "InsightsResponse",
    "LiveSnapshotResponse",
    "ModalOutcomeDistributionDto",
    "ModalOutcomeEntryDto",
    "OnboardingGateResponse",
    "PersonaSpecRequest",
    "ProjectedDecisionListResponse",
    "ProjectedDecisionResponse",
    "RehearsalLastRun",
    "RehearsalListItem",
    "RehearsalListResponse",
    "RollingAccuracyPointDto",
    "RollingAccuracySeriesDto",
    "SampledTrace",
    "ScenarioCatalogItem",
    "ScenarioCatalogResponse",
    "ScenarioRunListItemResponse",
    "ScenarioRunResponse",
    "SetForesightInsightsConfigRequest",
    "SetForesightThresholdRequest",
    "TierMixActual",
]
