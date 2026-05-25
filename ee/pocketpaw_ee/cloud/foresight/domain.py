# ee/pocketpaw_ee/cloud/foresight/domain.py
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

from dataclasses import dataclass
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


__all__ = [
    "BacktestRun",
    "BacktestRunStatus",
    "OnboardingGateState",
    "ProjectedDecision",
    "ScenarioRun",
    "ScenarioRunStatus",
]
