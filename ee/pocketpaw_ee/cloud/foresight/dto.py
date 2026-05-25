# ee/pocketpaw_ee/cloud/foresight/dto.py
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

from typing import Any

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
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    sub_type: str = Field(default="decision_forecast", max_length=64)
    n_ticks: int = Field(default=1, ge=1, le=1000)
    personas: list[PersonaSpecRequest] = Field(..., min_length=1, max_length=1000)
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


class BacktestRunResponse(BaseModel):
    """POST /backtests response + GET /backtests/:id response.

    Mirrors :class:`ScenarioRunResponse` plus two backtest-specific
    fields:

    - ``gate_decision``: ``ThresholdDecision.as_wire_dict()`` once the
      backtest completes (``None`` while queued / running / failed).
      The UI's Aggregate panel reads this directly to render the unlock
      label without re-computing.
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
    gate_decision: dict[str, Any] | None = None
    error: str | None = None


class BacktestRunListItemResponse(BaseModel):
    """Lighter shape for ``GET /backtests`` — drops the inline
    ``result`` / ``request`` blobs but keeps ``gate_decision`` so the
    list can render the unlock label per row without a click-through."""

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str | None = None
    scenario_name: str
    status: str
    created_at: str
    updated_at: str | None = None
    threshold: float
    gate_decision: dict[str, Any] | None = None
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


__all__ = [
    "BacktestRunListItemResponse",
    "BacktestRunResponse",
    "CreateBacktestRequest",
    "CreateScenarioRequest",
    "ForesightInstinctProposalListResponse",
    "ForesightInstinctProposalResponse",
    "HistoricalAnchorRequest",
    "OnboardingGateResponse",
    "PersonaSpecRequest",
    "ProjectedDecisionListResponse",
    "ProjectedDecisionResponse",
    "ScenarioRunListItemResponse",
    "ScenarioRunResponse",
]
