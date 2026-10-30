# tests/cloud/test_foresight_service_aggregate.py
# Created: 2026-05-26 (feat/foresight-v10-prediction-record-persist) —
# RFC 08 v1.0 PR 10. Verifies the rewritten ``get_aggregate_rollup``
# reads from the new ``foresight_prediction_records`` collection
# instead of the v0.5 ``ForesightBacktest.gate_decision.observed`` /
# ``ForesightProjectedDecision.confidence`` proxies. Wire shape is
# unchanged — the swap is a pure data-source change.
"""Tests for ``get_aggregate_rollup`` reading PredictionRecord docs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.foresight import service as foresight_service
from pocketpaw_ee.cloud.models.foresight_backtest import ForesightBacktest
from pocketpaw_ee.cloud.models.foresight_prediction_record import (
    ForesightPredictionRecord,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


async def _seed_paired_record(
    *,
    workspace: str,
    captured_at: datetime,
    pair_delta: dict[str, Any] | None = None,
    confidence: float = 0.7,
    modal_outcome: str = "accept",
    persona_id: str = "p-anne",
    anchor_id: str = "decision:lease-renewal",
    run_id: str = "67000000000000000000000a",
    tick_id: int = 0,
) -> ForesightPredictionRecord:
    """Direct doc-level seeder bypassing the service so we can control
    ``captured_at`` (the service stamps it from ``datetime.now``)."""
    if pair_delta is None:
        pair_delta = {"outcome": {"match": True, "projected": "accept", "actual": "accept"}}
    doc = ForesightPredictionRecord(
        workspace=workspace,
        anchor_id=anchor_id,
        persona_id=persona_id,
        scenario_id="seed-scn",
        run_id=run_id,
        tick_id=tick_id,
        prediction={"modal_outcome": modal_outcome},
        confidence=confidence,
        captured_at=captured_at,
        observed_at=captured_at + timedelta(seconds=1),
        observed_outcome={"outcome": modal_outcome},
        paired=True,
        pair_delta=pair_delta,
    )
    await doc.insert()
    return doc


# ---------------------------------------------------------------------------
# Reads PredictionRecord (NOT ForesightBacktest proxy)
# ---------------------------------------------------------------------------


async def test_aggregate_reads_prediction_records_not_backtest_proxy() -> None:
    """The signature of the proxy-to-real swap: a workspace with NO
    backtest docs but WITH paired PredictionRecord docs must still
    populate ``rolling_accuracy.points``. v0.5's proxy required the
    backtest gate_decision.observed field — the v1.0 path reads
    PredictionRecord.pair_delta directly.
    """
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    # Seed 3 paired records — all matches.
    for i in range(3):
        await _seed_paired_record(
            workspace="w1",
            captured_at=yesterday,
            run_id=f"67000000000000000000000{i}",
        )

    # Verify NO ForesightBacktest docs exist — this is the load-bearing
    # assertion: the v0.5 proxy would have returned an empty
    # rolling_accuracy here.
    backtests = await ForesightBacktest.find_all().to_list()
    assert backtests == []

    rollup = await foresight_service.get_aggregate_rollup(_ctx())
    # Real PredictionRecord-driven rolling accuracy → 3 matches in
    # 1 bucket = 100% accuracy, sample_count=3.
    assert len(rollup.rolling_accuracy.points) == 1
    point = rollup.rolling_accuracy.points[0]
    assert point.sample_count == 3
    assert point.accuracy == pytest.approx(1.0, abs=0.001)


async def test_aggregate_rolling_accuracy_mixes_match_and_mismatch() -> None:
    """A bucket with 2 matches + 1 mismatch yields ~0.667 accuracy
    derived from PredictionRecord.pair_delta, not gate_decision."""
    captured_at = datetime.now(UTC) - timedelta(days=1)
    await _seed_paired_record(
        workspace="w1",
        captured_at=captured_at,
        run_id="r-1",
    )
    await _seed_paired_record(
        workspace="w1",
        captured_at=captured_at,
        run_id="r-2",
    )
    # One mismatch — pair_delta carries match=False.
    await _seed_paired_record(
        workspace="w1",
        captured_at=captured_at,
        run_id="r-3",
        pair_delta={"outcome": {"match": False, "projected": "accept", "actual": "reject"}},
    )

    rollup = await foresight_service.get_aggregate_rollup(_ctx())
    assert len(rollup.rolling_accuracy.points) == 1
    point = rollup.rolling_accuracy.points[0]
    assert point.sample_count == 3
    assert point.accuracy == pytest.approx(2 / 3, abs=0.01)


async def test_aggregate_modal_distribution_from_prediction_records() -> None:
    """Modal-outcome share tally now sources from
    PredictionRecord.prediction.modal_outcome — NOT
    ForesightProjectedDecision.decision_text. v0.5 proxied the
    distribution; v1.0 reads the prediction blob directly."""
    captured_at = datetime.now(UTC) - timedelta(days=1)
    # 2 "accept" + 1 "reject"
    await _seed_paired_record(
        workspace="w1",
        captured_at=captured_at,
        run_id="r-1",
        modal_outcome="accept",
    )
    await _seed_paired_record(
        workspace="w1",
        captured_at=captured_at,
        run_id="r-2",
        modal_outcome="accept",
    )
    await _seed_paired_record(
        workspace="w1",
        captured_at=captured_at,
        run_id="r-3",
        modal_outcome="reject",
    )

    rollup = await foresight_service.get_aggregate_rollup(_ctx())
    entries = rollup.modal_outcome_distribution.entries
    assert len(entries) == 2
    by_outcome = {e.outcome: e.share for e in entries}
    assert by_outcome["accept"] == pytest.approx(2 / 3, abs=0.01)
    assert by_outcome["reject"] == pytest.approx(1 / 3, abs=0.01)


async def test_aggregate_unpaired_records_excluded_from_rolling_accuracy() -> None:
    """Unpaired records (forward sims with no observation) MUST NOT
    inflate the rolling-accuracy denominator. Only paired=True rows
    feed the series — this is the load-bearing filter in PR 10."""
    captured_at = datetime.now(UTC) - timedelta(days=1)
    # 2 paired matches + 5 unpaired forward-sim records.
    for i in range(2):
        await _seed_paired_record(
            workspace="w1",
            captured_at=captured_at,
            run_id=f"paired-r-{i}",
        )
    # Unpaired records — insert with paired=False.
    for i in range(5):
        unpaired = ForesightPredictionRecord(
            workspace="w1",
            anchor_id=f"decision:projection-{i}",
            persona_id="p-anne",
            scenario_id="seed-scn",
            run_id=f"unpaired-r-{i}",
            tick_id=0,
            prediction={"modal_outcome": "accept"},
            confidence=0.5,
            captured_at=captured_at,
            paired=False,
        )
        await unpaired.insert()

    rollup = await foresight_service.get_aggregate_rollup(_ctx())
    # Rolling-accuracy series only counts the 2 paired matches.
    assert len(rollup.rolling_accuracy.points) == 1
    point = rollup.rolling_accuracy.points[0]
    assert point.sample_count == 2
    assert point.accuracy == pytest.approx(1.0, abs=0.001)


async def test_aggregate_confidence_drift_reads_prediction_records() -> None:
    """Confidence-drift now reads PredictionRecord.confidence across
    day buckets. v0.5 proxied this from
    ForesightBacktest.result.calibration_summary.confidence_calibration."""
    now = datetime.now(UTC)
    earlier = now - timedelta(days=10)
    later = now - timedelta(days=1)
    # Earlier bucket: confidence 0.50
    await _seed_paired_record(
        workspace="w1",
        captured_at=earlier,
        run_id="early-1",
        confidence=0.50,
    )
    # Later bucket: confidence 0.85 (rising by 0.35 > 0.05 flat
    # threshold).
    await _seed_paired_record(
        workspace="w1",
        captured_at=later,
        run_id="late-1",
        confidence=0.85,
    )
    rollup = await foresight_service.get_aggregate_rollup(_ctx())
    assert rollup.confidence_drift.trend == "rising"
    assert rollup.confidence_drift.magnitude == pytest.approx(0.35, abs=0.01)


async def test_aggregate_empty_workspace_returns_zeros() -> None:
    """Empty workspace path still collapses to zeros + empty arrays —
    PR 10 must not regress the no-data behaviour."""
    rollup = await foresight_service.get_aggregate_rollup(_ctx())
    assert rollup.rolling_accuracy.points == []
    assert rollup.confidence_drift.trend == "flat"
    assert rollup.confidence_drift.magnitude == 0.0
    assert rollup.modal_outcome_distribution.entries == []


async def test_aggregate_window_filter_excludes_outside_records() -> None:
    """The window_days filter clips PredictionRecord reads by
    ``captured_at``. A record older than the window is invisible."""
    captured_in_window = datetime.now(UTC) - timedelta(days=2)
    captured_outside_window = datetime.now(UTC) - timedelta(days=20)
    await _seed_paired_record(
        workspace="w1",
        captured_at=captured_in_window,
        run_id="r-inside",
    )
    await _seed_paired_record(
        workspace="w1",
        captured_at=captured_outside_window,
        run_id="r-outside",
    )
    rollup = await foresight_service.get_aggregate_rollup(_ctx(), window_days=7)
    assert len(rollup.rolling_accuracy.points) == 1
    assert rollup.rolling_accuracy.points[0].sample_count == 1


async def test_aggregate_response_shape_unchanged() -> None:
    """Wire-shape backward-compat — the response must carry the same
    field structure v0.5 emitted (UI doesn't move)."""
    captured_at = datetime.now(UTC) - timedelta(days=1)
    await _seed_paired_record(
        workspace="w1",
        captured_at=captured_at,
        run_id="r-shape",
    )
    rollup = await foresight_service.get_aggregate_rollup(_ctx())
    # Wire shape is locked at the DTO layer — assert the response
    # carries every field the v0.5 router contract advertised.
    payload = rollup.model_dump()
    assert set(payload.keys()) == {
        "window_days",
        "generated_at",
        "rolling_accuracy",
        "confidence_drift",
        "modal_outcome_distribution",
    }
    assert set(payload["rolling_accuracy"].keys()) == {"points"}
    assert set(payload["confidence_drift"].keys()) == {"trend", "magnitude"}
    assert set(payload["modal_outcome_distribution"].keys()) == {"entries"}
    # ISO-8601 UTC timestamp.
    assert payload["generated_at"].endswith("Z")
