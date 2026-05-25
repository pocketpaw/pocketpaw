# tests/cloud/test_foresight_domain.py — RFC 08 PR 7.
# Created: 2026-05-25 (feat/foresight-v07-cloud-mount) — domain-layer
#   tests for the frozen value objects. No Mongo / Beanie / FastAPI;
#   asserts the cloud-rule #3 tenancy invariant (workspace_id required
#   at construction) and the ProjectedDecision field set per RFC §7.7.
"""Domain-layer tests for ``ee.cloud.foresight.domain``."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.foresight.domain import ProjectedDecision, ScenarioRun


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
    """RFC §7.7: a ProjectedDecision is a Decision plus three extras —
    ``run_id``, ``sim_tick``, ``projection_confidence``. Freeze the
    contract now so PR 8's persistence layer doesn't have to reshape."""
    pd = ProjectedDecision(
        id="proj-1",
        workspace_id="w1",
        run_id="run-1",
        sim_tick=10,
        anchor_object_id="lease:LR-2026-117",
        payload={"outcome": "accept", "price": 2850},
        projection_confidence=0.78,
        actors=("renewal_specialist", "approver_prakash"),
    )
    assert pd.run_id == "run-1"
    assert pd.sim_tick == 10
    assert 0.0 < pd.projection_confidence < 1.0
    assert pd.anchor_object_id == "lease:LR-2026-117"


def test_projected_decision_is_frozen() -> None:
    pd = ProjectedDecision(
        id="p",
        workspace_id="w1",
        run_id="r",
        sim_tick=0,
        anchor_object_id="x",
        payload={},
    )
    with pytest.raises(FrozenInstanceError):
        pd.projection_confidence = 0.9  # type: ignore[misc]


def test_projected_decision_requires_workspace_id() -> None:
    with pytest.raises(TypeError):
        ProjectedDecision(  # type: ignore[call-arg]
            id="p",
            run_id="r",
            sim_tick=0,
            anchor_object_id="x",
            payload={},
        )
