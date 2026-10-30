# tests/cloud/test_foresight_domain.py
# Modified: 2026-05-25 (feat/foresight-v04-backtest-aggregator) — PR 4
#   adds frozen-domain coverage for BacktestRun + OnboardingGateState:
#   tenancy invariant, immutability, optional vs required fields.
# Created: 2026-05-25 (feat/foresight-v07-cloud-mount) — domain-layer
#   tests for the frozen value objects. No Mongo / Beanie / FastAPI;
#   asserts the cloud-rule #3 tenancy invariant (workspace_id required
#   at construction) and the ProjectedDecision field set per RFC §7.7.
"""Domain-layer tests for ``ee.cloud.foresight.domain``."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.foresight.domain import (
    BacktestRun,
    OnboardingGateState,
    ProjectedDecision,
    ScenarioRun,
)


def _scenario_run(**overrides) -> ScenarioRun:
    defaults = {
        "id": "abc",
        "workspace_id": "w1",
        "scenario_name": "test",
        "status": "complete",
        "created_at": datetime.now(UTC),
        "request": {},
    }
    defaults.update(overrides)
    return ScenarioRun(**defaults)


def test_scenario_run_is_frozen() -> None:
    """Frozen dataclass — mutating after construction is a TypeError.

    Mirrors the cycles + notifications domain convention: value objects
    are immutable so the service can pass them around without worrying
    about callers mutating state behind its back.
    """
    run = _scenario_run()
    with pytest.raises(FrozenInstanceError):
        run.status = "failed"  # type: ignore[misc]


def test_scenario_run_requires_workspace_id() -> None:
    """Cloud rule #3: tenancy is enforced at construction. No default
    for ``workspace_id`` means TypeError when omitted."""
    with pytest.raises(TypeError):
        ScenarioRun(  # type: ignore[call-arg]
            id="abc",
            scenario_name="test",
            status="complete",
            created_at=datetime.now(UTC),
            request={},
        )


def test_scenario_run_carries_optional_result_and_error() -> None:
    run = _scenario_run(result={"actions_logged": 5}, error=None)
    assert run.result == {"actions_logged": 5}
    assert run.error is None

    failed = _scenario_run(status="failed", error="engine outage", result=None)
    assert failed.status == "failed"
    assert failed.error == "engine outage"


def test_projected_decision_carries_rfc_extras() -> None:
    """RFC §7.7: a ProjectedDecision carries the per-anchor projection
    fields PR 5 added — anchor_id, persona_id, tick_id, decision_text,
    confidence, sub_type, run_id. Tenancy (``workspace_id``) is the
    cloud rule #3 invariant. ``forward_precedent_decision_id`` is
    reserved for the RFC 07 Decision Graph backfill — None in v0.5."""
    pd = ProjectedDecision(
        id="proj-1",
        workspace_id="w1",
        run_id="run-1",
        anchor_id="decision:LR-2026-117",
        persona_id="persona-anne",
        tick_id=10,
        decision_text="accept",
        confidence=0.78,
        sub_type="decision_forecast",
    )
    assert pd.run_id == "run-1"
    assert pd.anchor_id == "decision:LR-2026-117"
    assert pd.tick_id == 10
    assert 0.0 < pd.confidence < 1.0
    assert pd.decision_text == "accept"
    assert pd.forward_precedent_decision_id is None


def test_projected_decision_is_frozen() -> None:
    pd = ProjectedDecision(
        id="p",
        workspace_id="w1",
        run_id="r",
        anchor_id="a",
        tick_id=0,
        decision_text="noop",
        confidence=0.0,
        sub_type="decision_forecast",
    )
    with pytest.raises(FrozenInstanceError):
        pd.confidence = 0.9  # type: ignore[misc]


def test_projected_decision_requires_workspace_id() -> None:
    with pytest.raises(TypeError):
        ProjectedDecision(  # type: ignore[call-arg]
            id="p",
            run_id="r",
            anchor_id="a",
            tick_id=0,
            decision_text="noop",
            confidence=0.0,
            sub_type="decision_forecast",
        )


# --- BacktestRun ----------------------------------------------------


def _backtest_run(**overrides) -> BacktestRun:
    defaults = {
        "id": "abc",
        "workspace_id": "w1",
        "scenario_name": "onboarding-backtest",
        "status": "complete",
        "created_at": datetime.now(UTC),
        "request": {},
        "threshold": 0.65,
    }
    defaults.update(overrides)
    return BacktestRun(**defaults)


def test_backtest_run_is_frozen() -> None:
    run = _backtest_run()
    with pytest.raises(FrozenInstanceError):
        run.status = "failed"  # type: ignore[misc]


def test_backtest_run_requires_workspace_id() -> None:
    """Cloud rule #3: tenancy enforced at construction."""
    with pytest.raises(TypeError):
        BacktestRun(  # type: ignore[call-arg]
            id="abc",
            scenario_name="x",
            status="complete",
            created_at=datetime.now(UTC),
            request={},
            threshold=0.65,
        )


def test_backtest_run_carries_gate_decision_and_threshold() -> None:
    run = _backtest_run(
        gate_decision={"passed": True, "observed": 0.72, "threshold": 0.65, "margin": 0.07},
        threshold=0.65,
    )
    assert run.gate_decision is not None
    assert run.gate_decision["passed"] is True
    assert run.threshold == 0.65


def test_backtest_run_supports_failed_state() -> None:
    failed = _backtest_run(status="failed", error="engine outage", result=None, gate_decision=None)
    assert failed.status == "failed"
    assert failed.error == "engine outage"


# --- OnboardingGateState --------------------------------------------


def test_onboarding_gate_state_unlocked_carries_backtest_ref() -> None:
    state = OnboardingGateState(
        workspace_id="w1",
        unlocked=True,
        threshold=0.65,
        reason="unlocked",
        last_backtest_id="bt-1",
        last_backtest_accuracy=0.72,
        last_backtest_at=datetime.now(UTC),
    )
    assert state.unlocked is True
    assert state.reason == "unlocked"
    assert state.last_backtest_id == "bt-1"


def test_onboarding_gate_state_closed_no_backtest_has_no_ref() -> None:
    state = OnboardingGateState(
        workspace_id="w1",
        unlocked=False,
        threshold=0.65,
        reason="no_backtest",
    )
    assert state.unlocked is False
    assert state.last_backtest_id is None
    assert state.last_backtest_accuracy is None


def test_onboarding_gate_state_is_frozen() -> None:
    state = OnboardingGateState(
        workspace_id="w1",
        unlocked=False,
        threshold=0.65,
        reason="no_backtest",
    )
    with pytest.raises(FrozenInstanceError):
        state.unlocked = True  # type: ignore[misc]


def test_onboarding_gate_state_requires_workspace_id() -> None:
    with pytest.raises(TypeError):
        OnboardingGateState(  # type: ignore[call-arg]
            unlocked=False,
            threshold=0.65,
            reason="no_backtest",
        )
