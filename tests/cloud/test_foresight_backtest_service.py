# tests/cloud/test_foresight_backtest_service.py — RFC 08 PR 4.
# Updated: 2026-05-26 (feat/foresight-v10-live-snapshot-and-fixes) —
#   ``gate_decision`` is now a :class:`GateDecision` Pydantic model
#   instead of a free-form dict; updated the ``persists_backtest_with_tenancy``
#   assertion to use attribute access. The legacy dict shape stays
#   available via ``model_dump()`` so the rest of the suite (event
#   payload reads via ``model_dump``) keeps working unchanged.
# Updated: 2026-05-26 (feat/foresight-v10-prediction-record-persist) —
#   monkeypatched ``_score_backtest`` fakes accept ``**_kwargs`` to
#   absorb the new ``workspace_id`` + ``backtest_run_id`` kwargs PR 10
#   threads through for paired PredictionRecord persistence. Behaviour
#   under test (gate state machine, unlock event) is unaffected.
# Created: 2026-05-25 (feat/foresight-v04-backtest-aggregator) — service
#   tests for the retroactive backtest gate. Exercises:
#     - create → score → emit pipeline (queued / running / complete)
#     - threshold tightening allowed, relaxation blocked
#     - engine failure captured as ``failed`` with ``ForesightBacktestFailed``
#     - tenancy isolation (cross-workspace read = 404)
#     - get_onboarding_gate state machine
#         (no_backtest / in_flight / below_threshold / unlocked)
#     - ForesightOnboardingUnlocked fires only on pass
"""Tests for ``ee.cloud.foresight.service`` backtest API."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.events import (
    ForesightBacktestCompleted,
    ForesightBacktestCreated,
    ForesightBacktestFailed,
    ForesightOnboardingUnlocked,
)
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.foresight.dto import (
    CreateBacktestRequest,
    HistoricalAnchorRequest,
    PersonaSpecRequest,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace: str | None = "w1", user: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _body(
    *,
    name: str = "onboarding-backtest",
    anchors: list[HistoricalAnchorRequest] | None = None,
    threshold: float | None = None,
) -> CreateBacktestRequest:
    """Backtest body with N anchors. Default anchors all return
    ``{"outcome": "accept"}`` — the engine's deterministic fake doesn't
    fan per-anchor projections yet, so pairs reduce to outcome matches
    against a missing projection (count as mismatches in v0.1).
    """
    return CreateBacktestRequest(
        name=name,
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[
            PersonaSpecRequest(name="Anne", role="approver", ocean={}),
        ],
        anchors=anchors
        or [
            HistoricalAnchorRequest(
                anchor_object_id=f"lease:LR-{i}",
                actual_outcome={"outcome": "accept"},
            )
            for i in range(10)
        ],
        threshold=threshold,
    )


# ---------------------------------------------------------------------------
# Create + persist + emit
# ---------------------------------------------------------------------------


async def test_create_persists_backtest_with_tenancy(recording_bus) -> None:
    ctx = _ctx(workspace="w1", user="shawn")
    out = await foresight_service.create_backtest(ctx, _body(name="q3-backtest"))

    assert out.workspace_id == "w1"
    assert out.scenario_name == "q3-backtest"
    assert out.status == "complete"
    assert out.threshold == 0.65  # default
    assert out.result is not None
    assert "calibration_summary" in out.result
    assert out.gate_decision is not None
    # v1.0 promoted ``gate_decision`` from ``dict[str, Any]`` to a
    # :class:`GateDecision` Pydantic model. The legacy field set
    # (``passed`` / ``threshold``) is preserved on the model.
    assert out.gate_decision.passed is not None
    assert isinstance(out.gate_decision.threshold, float)
    # Pydantic's ``model_dump`` exposes the legacy dict shape for any
    # downstream caller that keyed on the flat dict.
    serialized = out.gate_decision.model_dump()
    assert "passed" in serialized
    assert "threshold" in serialized
    assert "reason" in serialized
    assert "evaluated_at" in serialized


async def test_create_emits_created_then_completed(recording_bus) -> None:
    ctx = _ctx()
    out = await foresight_service.create_backtest(ctx, _body())

    created = [e for e in recording_bus.events if isinstance(e, ForesightBacktestCreated)]
    completed = [e for e in recording_bus.events if isinstance(e, ForesightBacktestCompleted)]
    assert len(created) == 1
    assert len(completed) == 1
    assert created[0].data["id"] == out.id
    assert created[0].data["status"] == "queued"
    assert completed[0].data["id"] == out.id
    assert completed[0].data["status"] == "complete"


async def test_create_fires_unlock_event_when_gate_passes(recording_bus) -> None:
    """When the aggregator's accuracy clears the threshold, the
    backtest emits BOTH completed + onboarding_unlocked. The latter
    is the chat-agent skill's trigger."""
    ctx = _ctx()
    # Use a low threshold so the v0.1 placeholder pairing trivially
    # passes; with a 0.0 threshold any modal accuracy >= 0 unlocks.
    # 0.65 is the floor — a per-run threshold below the floor is
    # blocked. The unlock path is exercised through a permissive
    # synthetic scenario in test_unlock_event_path below.
    body = _body(threshold=0.65)
    await foresight_service.create_backtest(ctx, body)
    completed = [e for e in recording_bus.events if isinstance(e, ForesightBacktestCompleted)]
    assert len(completed) == 1
    # Whether the unlock event fires depends on whether the v0.1
    # placeholder pairing scored above 0.65; verify the contract: the
    # gate decision in the completed event is the source of truth and
    # the unlock event count matches that.
    gate = completed[0].data["gate_decision"]
    unlocked = [e for e in recording_bus.events if isinstance(e, ForesightOnboardingUnlocked)]
    assert (len(unlocked) == 1) == bool(gate.get("passed"))


async def test_create_unlock_event_carries_workspace_and_accuracy(
    recording_bus, monkeypatch
) -> None:
    """Pin the unlock-event payload shape by forcing a passing gate
    via a monkeypatched scorer — this isolates the test from whatever
    v0.1's placeholder pairing happens to score."""
    ctx = _ctx(workspace="ws-unlock-test")

    async def _passing_scorer(_body, *, engine_result, threshold, **_kwargs):
        return (
            {"modal_accuracy": 0.8, "confidence_calibration": 1.0, "n_pairs": 10},
            {
                "passed": True,
                "observed": 0.8,
                "threshold": threshold,
                "margin": 0.8 - threshold,
                "n_pairs": 10,
            },
        )

    monkeypatch.setattr(foresight_service, "_score_backtest", _passing_scorer)

    out = await foresight_service.create_backtest(ctx, _body(name="pass"))

    unlocked = [e for e in recording_bus.events if isinstance(e, ForesightOnboardingUnlocked)]
    assert len(unlocked) == 1
    payload = unlocked[0].data
    assert payload["workspace_id"] == "ws-unlock-test"
    assert payload["backtest_id"] == out.id
    assert payload["threshold"] == 0.65
    assert payload["accuracy"] == pytest.approx(0.8)


async def test_create_no_unlock_event_when_gate_fails(recording_bus, monkeypatch) -> None:
    """Failing gate should NOT fire the unlock event."""
    ctx = _ctx()

    async def _failing_scorer(_body, *, engine_result, threshold, **_kwargs):
        return (
            {"modal_accuracy": 0.3, "confidence_calibration": 0.5, "n_pairs": 10},
            {
                "passed": False,
                "observed": 0.3,
                "threshold": threshold,
                "margin": 0.3 - threshold,
                "n_pairs": 10,
            },
        )

    monkeypatch.setattr(foresight_service, "_score_backtest", _failing_scorer)
    await foresight_service.create_backtest(ctx, _body(name="fail"))

    unlocked = [e for e in recording_bus.events if isinstance(e, ForesightOnboardingUnlocked)]
    assert unlocked == []


# ---------------------------------------------------------------------------
# Threshold floor (RFC §13.1 gate 7 — captain locked default 0.65)
# ---------------------------------------------------------------------------


async def test_create_allows_threshold_tightening_above_default() -> None:
    ctx = _ctx()
    out = await foresight_service.create_backtest(ctx, _body(threshold=0.80))
    assert out.threshold == 0.80


async def test_create_rejects_threshold_relaxation_below_default() -> None:
    ctx = _ctx()
    with pytest.raises(ValidationError) as exc:
        await foresight_service.create_backtest(ctx, _body(threshold=0.40))
    assert exc.value.code == "foresight.threshold_below_default"


async def test_create_uses_default_threshold_when_none() -> None:
    ctx = _ctx()
    out = await foresight_service.create_backtest(ctx, _body(threshold=None))
    assert out.threshold == 0.65


# ---------------------------------------------------------------------------
# Engine failure
# ---------------------------------------------------------------------------


async def test_create_captures_engine_failure_as_failed_status(monkeypatch, recording_bus) -> None:
    ctx = _ctx()

    async def _boom(_body):
        raise RuntimeError("simulated engine outage")

    monkeypatch.setattr(foresight_service, "_run_engine_inline", _boom)

    out = await foresight_service.create_backtest(ctx, _body(name="boom-backtest"))

    assert out.status == "failed"
    assert out.error is not None
    assert "simulated engine outage" in out.error
    assert out.gate_decision is None

    failed = [e for e in recording_bus.events if isinstance(e, ForesightBacktestFailed)]
    assert len(failed) == 1
    assert failed[0].data["id"] == out.id


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_create_rejects_unsupported_sub_type() -> None:
    ctx = _ctx()
    body = CreateBacktestRequest(
        name="ops-stress-backtest",
        sub_type="ops_stress_test",
        n_ticks=1,
        personas=[PersonaSpecRequest(name="A", role="participant", ocean={})],
        anchors=[
            HistoricalAnchorRequest(
                anchor_object_id="x:1",
                actual_outcome={"outcome": "accept"},
            )
        ],
    )
    with pytest.raises(ValidationError) as exc:
        await foresight_service.create_backtest(ctx, body)
    assert exc.value.code == "foresight.invalid_scenario"


async def test_create_requires_workspace() -> None:
    ctx = _ctx(workspace=None)
    with pytest.raises(Forbidden) as exc:
        await foresight_service.create_backtest(ctx, _body())
    assert exc.value.code == "foresight.no_workspace"


# ---------------------------------------------------------------------------
# Get + tenancy
# ---------------------------------------------------------------------------


async def test_get_returns_same_payload() -> None:
    ctx = _ctx()
    created = await foresight_service.create_backtest(ctx, _body(name="echo"))

    fetched = await foresight_service.get_backtest(ctx, created.id)
    assert fetched.id == created.id
    assert fetched.scenario_name == "echo"
    assert fetched.gate_decision == created.gate_decision


async def test_get_404_for_unknown_id() -> None:
    ctx = _ctx()
    with pytest.raises(NotFound):
        await foresight_service.get_backtest(ctx, "5f50c31b1c9d440000000000")


async def test_get_404_for_malformed_id() -> None:
    ctx = _ctx()
    with pytest.raises(NotFound):
        await foresight_service.get_backtest(ctx, "not-an-objectid")


async def test_get_isolates_across_workspaces() -> None:
    ctx_w1 = _ctx(workspace="w1")
    ctx_w2 = _ctx(workspace="w2")
    created = await foresight_service.create_backtest(ctx_w1, _body(name="private"))

    with pytest.raises(NotFound):
        await foresight_service.get_backtest(ctx_w2, created.id)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


async def test_list_returns_newest_first() -> None:
    ctx = _ctx()
    first = await foresight_service.create_backtest(ctx, _body(name="bt-1"))
    second = await foresight_service.create_backtest(ctx, _body(name="bt-2"))
    third = await foresight_service.create_backtest(ctx, _body(name="bt-3"))

    items = await foresight_service.list_backtests(ctx)
    assert [i.id for i in items] == [third.id, second.id, first.id]


async def test_list_keeps_gate_decision_drops_result_blob() -> None:
    """List shape carries ``gate_decision`` so the Aggregate panel can
    render the pass/fail label per row; drops ``result`` to keep the
    payload small."""
    ctx = _ctx()
    await foresight_service.create_backtest(ctx, _body())

    items = await foresight_service.list_backtests(ctx)
    assert len(items) == 1
    serialized = items[0].model_dump()
    assert "result" not in serialized
    assert "request" not in serialized
    assert "gate_decision" in serialized
    assert "threshold" in serialized


async def test_list_isolates_across_workspaces() -> None:
    ctx_w1 = _ctx(workspace="w1")
    ctx_w2 = _ctx(workspace="w2")
    await foresight_service.create_backtest(ctx_w1, _body(name="w1-only"))
    await foresight_service.create_backtest(ctx_w2, _body(name="w2-only"))

    w1_items = await foresight_service.list_backtests(ctx_w1)
    w2_items = await foresight_service.list_backtests(ctx_w2)

    assert {i.scenario_name for i in w1_items} == {"w1-only"}
    assert {i.scenario_name for i in w2_items} == {"w2-only"}


async def test_list_requires_workspace() -> None:
    ctx = _ctx(workspace=None)
    with pytest.raises(Forbidden):
        await foresight_service.list_backtests(ctx)


async def test_list_rejects_invalid_limit() -> None:
    ctx = _ctx()
    with pytest.raises(ValidationError):
        await foresight_service.list_backtests(ctx, limit=0)


async def test_list_offset_skips_initial_backtests() -> None:
    """``offset`` mirrors the cursor pattern on ``list_scenario_runs``
    so the agent_context wrapper paginates server-side instead of
    over-fetching and slicing. Five backtests in newest-first order:
    ``skip=2, limit=2`` returns backtests 3-4 — i.e. the third and fourth
    newest, after the latest two were skipped."""
    ctx = _ctx()
    backtests = []
    for i in range(5):
        backtests.append(await foresight_service.create_backtest(ctx, _body(name=f"bt-{i}")))

    page = await foresight_service.list_backtests(ctx, limit=2, offset=2)
    # Newest-first: backtests[4], backtests[3], backtests[2], backtests[1],
    # backtests[0]. offset=2 drops the first two (backtests[4],
    # backtests[3]); limit=2 returns backtests[2], backtests[1].
    assert [item.id for item in page] == [backtests[2].id, backtests[1].id]


async def test_list_rejects_negative_offset() -> None:
    ctx = _ctx()
    with pytest.raises(ValidationError):
        await foresight_service.list_backtests(ctx, offset=-1)


# ---------------------------------------------------------------------------
# get_onboarding_gate state machine
# ---------------------------------------------------------------------------


async def test_gate_reports_no_backtest_when_workspace_is_empty() -> None:
    ctx = _ctx(workspace="fresh-ws")
    gate = await foresight_service.get_onboarding_gate(ctx)
    assert gate.workspace_id == "fresh-ws"
    assert gate.unlocked is False
    assert gate.reason == "no_backtest"
    assert gate.threshold == 0.65
    assert gate.last_backtest_id is None


async def test_gate_reports_unlocked_after_passing_backtest(monkeypatch) -> None:
    ctx = _ctx(workspace="pass-ws")

    async def _passing_scorer(_body, *, engine_result, threshold, **_kwargs):
        return (
            {"modal_accuracy": 0.8, "n_pairs": 10},
            {
                "passed": True,
                "observed": 0.8,
                "threshold": threshold,
                "margin": 0.15,
                "n_pairs": 10,
            },
        )

    monkeypatch.setattr(foresight_service, "_score_backtest", _passing_scorer)

    out = await foresight_service.create_backtest(ctx, _body())
    gate = await foresight_service.get_onboarding_gate(ctx)
    assert gate.unlocked is True
    assert gate.reason == "unlocked"
    assert gate.last_backtest_id == out.id
    assert gate.last_backtest_accuracy == pytest.approx(0.8)


async def test_gate_reports_below_threshold_after_failing_backtest(monkeypatch) -> None:
    ctx = _ctx(workspace="fail-ws")

    async def _failing_scorer(_body, *, engine_result, threshold, **_kwargs):
        return (
            {"modal_accuracy": 0.3, "n_pairs": 10},
            {
                "passed": False,
                "observed": 0.3,
                "threshold": threshold,
                "margin": -0.35,
                "n_pairs": 10,
            },
        )

    monkeypatch.setattr(foresight_service, "_score_backtest", _failing_scorer)

    await foresight_service.create_backtest(ctx, _body())
    gate = await foresight_service.get_onboarding_gate(ctx)
    assert gate.unlocked is False
    assert gate.reason == "below_threshold"
    assert gate.last_backtest_accuracy == pytest.approx(0.3)


async def test_gate_isolates_across_workspaces(monkeypatch) -> None:
    """A passing backtest in w1 must NOT unlock the gate for w2."""
    ctx_w1 = _ctx(workspace="w1")
    ctx_w2 = _ctx(workspace="w2")

    async def _passing_scorer(_body, *, engine_result, threshold, **_kwargs):
        return (
            {"modal_accuracy": 0.9, "n_pairs": 10},
            {
                "passed": True,
                "observed": 0.9,
                "threshold": threshold,
                "margin": 0.25,
                "n_pairs": 10,
            },
        )

    monkeypatch.setattr(foresight_service, "_score_backtest", _passing_scorer)
    await foresight_service.create_backtest(ctx_w1, _body())

    gate_w1 = await foresight_service.get_onboarding_gate(ctx_w1)
    gate_w2 = await foresight_service.get_onboarding_gate(ctx_w2)
    assert gate_w1.unlocked is True
    assert gate_w2.unlocked is False
    assert gate_w2.reason == "no_backtest"


async def test_gate_requires_workspace() -> None:
    ctx = _ctx(workspace=None)
    with pytest.raises(Forbidden):
        await foresight_service.get_onboarding_gate(ctx)
