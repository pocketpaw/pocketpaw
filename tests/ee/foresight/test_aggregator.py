# tests/ee/foresight/test_aggregator.py
# Created: 2026-05-25 (feat/foresight-v04-backtest-aggregator) — RFC 08 PR 4.
#
# Pin the aggregator primitive contracts (RFC §9.4 / §11.4):
#   - accuracy_meets_threshold: passes when modal_accuracy >= threshold and
#     n_pairs >= min_pairs; fails on thin samples; rejects bad thresholds.
#   - summarize_by + group_pairs_by: split pairs by key_fn, hand each
#     bucket to aggregate_pairs, return per-group summaries.
#   - per_scenario_template_summary / per_anchor_namespace_summary:
#     concrete groupers using shipped PredictionRecord fields.
#   - rolling_accuracy / rolling_accuracy_series: time-windowed slicing
#     using pair.paired_at; deterministic with caller-supplied ``now``.
#   - confidence_drift: improving / degrading / flat trend with caller-
#     tunable flat_threshold.
#   - modal_outcome_distribution: per-value frequency table, projected
#     vs actual side.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pocketpaw_ee.foresight.aggregator import (
    ConfidenceDrift,
    ModalOutcomeDistribution,
    ThresholdDecision,
    accuracy_meets_threshold,
    confidence_drift,
    group_pairs_by,
    index_predictions,
    modal_outcome_distribution,
    per_anchor_namespace_summary,
    per_scenario_template_summary,
    rolling_accuracy,
    rolling_accuracy_series,
    summarize_by,
)
from pocketpaw_ee.foresight.calibration import (
    CalibrationSummary,
    PredictionRecord,
    aggregate_pairs,
    build_prediction_record,
    pair_against_reality,
)

# ---------------------------------------------------------------------------
# Helpers — build deterministic prediction + pair fixtures
# ---------------------------------------------------------------------------


def _prediction(
    *,
    template: str = "decision_forecast.yaml",
    anchor: str = "lease:LR-2026-117",
    projected: dict | None = None,
    confidence: float = 0.7,
) -> PredictionRecord:
    return build_prediction_record(
        scenario_template=template,
        run_id=uuid4(),
        anchor_object_id=anchor,
        projected_outcome=projected or {"outcome": "accept", "amount": 2850.0},
        observe_at=datetime.now(UTC),
        projection_confidence=confidence,
    )


def _pair_at(prediction: PredictionRecord, actual: dict, *, when: datetime):
    """Build a pair whose paired_at is deterministic (rolling tests need it)."""
    pair = pair_against_reality(prediction, actual_outcome=actual)
    # CalibrationPair is frozen; rebuild with a fixed paired_at.
    from dataclasses import replace

    return replace(pair, paired_at=when)


# ---------------------------------------------------------------------------
# accuracy_meets_threshold
# ---------------------------------------------------------------------------


def test_threshold_passes_when_accuracy_meets_bar():
    summary = CalibrationSummary(
        modal_accuracy=0.72,
        confidence_calibration=0.8,
        per_metric_accuracy={"outcome": 0.72},
        n_pairs=10,
    )
    decision = accuracy_meets_threshold(summary, threshold=0.65)
    assert isinstance(decision, ThresholdDecision)
    assert decision.passed is True
    assert decision.observed == pytest.approx(0.72)
    assert decision.threshold == pytest.approx(0.65)
    assert decision.margin == pytest.approx(0.07)


def test_threshold_fails_below_bar():
    summary = CalibrationSummary(
        modal_accuracy=0.5,
        confidence_calibration=0.4,
        per_metric_accuracy={},
        n_pairs=10,
    )
    decision = accuracy_meets_threshold(summary, threshold=0.65)
    assert decision.passed is False
    assert decision.margin == pytest.approx(-0.15)


def test_threshold_fails_when_n_pairs_below_min():
    """Thin samples can't unlock — even at perfect accuracy, fewer pairs
    than ``min_pairs`` keeps the gate closed (the backtest needs more
    historical anchors before we honestly trust the result)."""
    summary = CalibrationSummary(
        modal_accuracy=1.0,
        confidence_calibration=1.0,
        per_metric_accuracy={},
        n_pairs=3,
    )
    decision = accuracy_meets_threshold(summary, threshold=0.65, min_pairs=10)
    assert decision.passed is False
    assert decision.observed == pytest.approx(1.0)


def test_threshold_rejects_out_of_band_value():
    summary = CalibrationSummary(0.5, 0.5, {}, 1)
    with pytest.raises(ValueError, match="threshold must be"):
        accuracy_meets_threshold(summary, threshold=1.5)


def test_threshold_rejects_non_positive_min_pairs():
    summary = CalibrationSummary(0.5, 0.5, {}, 1)
    with pytest.raises(ValueError, match="min_pairs must be"):
        accuracy_meets_threshold(summary, threshold=0.5, min_pairs=0)


def test_threshold_decision_wire_dict_round_trips():
    summary = CalibrationSummary(0.72, 0.81, {"outcome": 0.72}, 10)
    decision = accuracy_meets_threshold(summary, threshold=0.65)
    wire = decision.as_wire_dict()
    assert wire == {
        "passed": True,
        "observed": 0.72,
        "threshold": 0.65,
        "margin": 0.07,
        "n_pairs": 10,
    }


# ---------------------------------------------------------------------------
# group_pairs_by / summarize_by
# ---------------------------------------------------------------------------


def test_group_pairs_by_splits_buckets_with_custom_key():
    pred_a = _prediction(anchor="lease:A")
    pred_b = _prediction(anchor="deal:B")
    pred_c = _prediction(anchor="lease:C")
    pairs = [
        pair_against_reality(pred_a, actual_outcome={"outcome": "accept"}),
        pair_against_reality(pred_b, actual_outcome={"outcome": "reject"}),
        pair_against_reality(pred_c, actual_outcome={"outcome": "accept"}),
    ]
    grouped = group_pairs_by(
        pairs,
        predictions_by_id=index_predictions([pred_a, pred_b, pred_c]),
        key_fn=lambda rec, _p: rec.anchor_object_id.split(":", 1)[0] if rec else "?",
    )
    assert set(grouped.keys()) == {"lease", "deal"}
    assert len(grouped["lease"]) == 2
    assert len(grouped["deal"]) == 1


def test_group_pairs_by_handles_missing_record_as_none():
    """When predictions_by_id is empty, ``record`` is None inside the
    key_fn — callers can still group on pair-only fields."""
    pred = _prediction()
    pair = pair_against_reality(pred, actual_outcome={"outcome": "accept"})
    grouped = group_pairs_by(
        [pair],
        predictions_by_id={},  # explicitly empty
        key_fn=lambda rec, _p: "missing" if rec is None else "present",
    )
    assert list(grouped.keys()) == ["missing"]


def test_summarize_by_returns_per_group_summary():
    # Use single-key projections so the pair's match flag reflects only
    # the outcome (matching pair.delta key set tolerates missing keys
    # but a "missing_in" delta never satisfies the match band).
    predictions = [
        _prediction(anchor=f"lease:{i}", projected={"outcome": "accept"}) for i in range(5)
    ]
    predictions += [
        _prediction(anchor=f"deal:{i}", projected={"outcome": "accept"}) for i in range(2)
    ]
    pairs = []
    # 4 of 5 leases match; 1 of 2 deals match.
    for i, pred in enumerate(predictions[:5]):
        actual = "accept" if i < 4 else "reject"
        pairs.append(pair_against_reality(pred, actual_outcome={"outcome": actual}))
    for i, pred in enumerate(predictions[5:]):
        actual = "accept" if i == 0 else "reject"
        pairs.append(pair_against_reality(pred, actual_outcome={"outcome": actual}))

    summaries = summarize_by(
        pairs,
        predictions_by_id=index_predictions(predictions),
        key_fn=lambda rec, _p: rec.anchor_object_id.split(":", 1)[0],
    )

    assert set(summaries.keys()) == {"lease", "deal"}
    assert summaries["lease"].modal_accuracy == pytest.approx(4 / 5)
    assert summaries["lease"].n_pairs == 5
    assert summaries["deal"].modal_accuracy == pytest.approx(1 / 2)
    assert summaries["deal"].n_pairs == 2


# ---------------------------------------------------------------------------
# per_scenario_template_summary / per_anchor_namespace_summary
# ---------------------------------------------------------------------------


def test_per_scenario_template_summary_buckets_by_template():
    pred_df = _prediction(template="decision_forecast.yaml")
    pred_ms = _prediction(template="market_sim.yaml")
    pairs = [
        pair_against_reality(pred_df, actual_outcome={"outcome": "accept"}),
        pair_against_reality(pred_ms, actual_outcome={"outcome": "reject"}),
    ]
    summaries = per_scenario_template_summary(
        pairs,
        predictions_by_id=index_predictions([pred_df, pred_ms]),
    )
    assert set(summaries.keys()) == {"decision_forecast.yaml", "market_sim.yaml"}
    assert summaries["decision_forecast.yaml"].n_pairs == 1
    assert summaries["market_sim.yaml"].n_pairs == 1


def test_per_scenario_template_summary_orphans_land_in_unknown_bucket():
    """Pairs whose prediction was purged still surface — they collapse
    to the sentinel ``<unknown>`` key so the caller sees the tail size."""
    pred = _prediction(template="decision_forecast.yaml")
    pair = pair_against_reality(pred, actual_outcome={"outcome": "accept"})
    # predictions_by_id intentionally missing this pair's record
    summaries = per_scenario_template_summary(
        [pair],
        predictions_by_id={},
    )
    assert "<unknown>" in summaries
    assert summaries["<unknown>"].n_pairs == 1


def test_per_anchor_namespace_summary_splits_lease_vs_deal():
    pred_l = _prediction(anchor="lease:LR-117")
    pred_d = _prediction(anchor="deal:OPP-44")
    pred_bare = _prediction(anchor="bare-anchor-no-colon")
    pairs = [
        pair_against_reality(pred_l, actual_outcome={"outcome": "accept"}),
        pair_against_reality(pred_d, actual_outcome={"outcome": "reject"}),
        pair_against_reality(pred_bare, actual_outcome={"outcome": "accept"}),
    ]
    summaries = per_anchor_namespace_summary(
        pairs,
        predictions_by_id=index_predictions([pred_l, pred_d, pred_bare]),
    )
    # Anchors with no ``:`` land under the bare string (not "<unknown>").
    assert set(summaries.keys()) == {"lease", "deal", "bare-anchor-no-colon"}


# ---------------------------------------------------------------------------
# rolling_accuracy / rolling_accuracy_series
# ---------------------------------------------------------------------------


def _ten_pairs_over_ten_days(now: datetime) -> list:
    """7 matching + 3 mismatching pairs, one per day going back 10 days
    (most recent first). Used by the rolling-window tests."""
    pairs = []
    for day, outcome in enumerate(["accept"] * 7 + ["reject"] * 3):  # 7 match, 3 mismatch
        pred = _prediction(projected={"outcome": "accept"})
        pair = _pair_at(
            pred,
            actual={"outcome": outcome},
            when=now - timedelta(days=day),
        )
        pairs.append(pair)
    return pairs


def test_rolling_accuracy_includes_only_pairs_inside_window():
    now = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    pairs = _ten_pairs_over_ten_days(now)
    # 4-day window: pairs at day 0,1,2,3,4 land inside [now-4d, now] —
    # both endpoints inclusive. All 5 of those are matching (the first
    # 7 of the 10 pairs are the matching ones).
    summary = rolling_accuracy(pairs, window=timedelta(days=4), now=now)
    assert summary.n_pairs == 5
    assert summary.modal_accuracy == pytest.approx(1.0)
    # 30-day window keeps all 10 → 70% accuracy (7 / 10).
    summary_all = rolling_accuracy(pairs, window=timedelta(days=30), now=now)
    assert summary_all.n_pairs == 10
    assert summary_all.modal_accuracy == pytest.approx(0.7)


def test_rolling_accuracy_empty_window_returns_zero_pairs():
    now = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    pairs = _ten_pairs_over_ten_days(now)
    # Look at a window 100 days ago — no pairs land in it.
    future_window = rolling_accuracy(
        pairs,
        window=timedelta(days=1),
        now=now - timedelta(days=100),
    )
    assert future_window.n_pairs == 0
    assert future_window.modal_accuracy == pytest.approx(0.0)


def test_rolling_accuracy_rejects_non_positive_window():
    with pytest.raises(ValueError, match="window must be"):
        rolling_accuracy([], window=timedelta(0))


def test_rolling_accuracy_series_produces_ordered_buckets():
    now = datetime(2026, 5, 25, 12, 0, tzinfo=UTC)
    pairs = _ten_pairs_over_ten_days(now)
    series = rolling_accuracy_series(
        pairs,
        window=timedelta(days=2),
        step=timedelta(days=2),
        now=now,
        horizon=timedelta(days=10),
    )
    # 10 days / 2-day step = 6 bucket ends (inclusive endpoint).
    assert len(series) == 6
    # Oldest first.
    ends = [end for end, _ in series]
    assert ends == sorted(ends)
    # Latest bucket sits at ``now`` and has the most-recent matching pairs.
    latest_end, latest_summary = series[-1]
    assert latest_end == now
    assert latest_summary.n_pairs >= 1


def test_rolling_accuracy_series_rejects_non_positive_step():
    with pytest.raises(ValueError, match="step must be"):
        rolling_accuracy_series([], window=timedelta(days=1), step=timedelta(0))


# ---------------------------------------------------------------------------
# confidence_drift
# ---------------------------------------------------------------------------


def test_confidence_drift_flags_improving_trend():
    summaries = [
        CalibrationSummary(0.6, 0.5, {}, 10),
        CalibrationSummary(0.7, 0.7, {}, 10),
        CalibrationSummary(0.7, 0.85, {}, 10),
    ]
    drift = confidence_drift(summaries)
    assert isinstance(drift, ConfidenceDrift)
    assert drift.trend == "improving"
    assert drift.delta == pytest.approx(0.35)
    assert drift.n_summaries == 3


def test_confidence_drift_flags_degrading_trend():
    summaries = [
        CalibrationSummary(0.7, 0.9, {}, 10),
        CalibrationSummary(0.6, 0.6, {}, 10),
    ]
    drift = confidence_drift(summaries)
    assert drift.trend == "degrading"
    assert drift.delta == pytest.approx(-0.30)


def test_confidence_drift_marks_small_movement_as_flat():
    summaries = [
        CalibrationSummary(0.7, 0.70, {}, 10),
        CalibrationSummary(0.7, 0.71, {}, 10),
    ]
    drift = confidence_drift(summaries, flat_threshold=0.02)
    assert drift.trend == "flat"


def test_confidence_drift_empty_input_is_flat_zero():
    drift = confidence_drift([])
    assert drift.trend == "flat"
    assert drift.delta == 0.0
    assert drift.n_summaries == 0


def test_confidence_drift_single_point_is_flat():
    summaries = [CalibrationSummary(0.7, 0.5, {}, 10)]
    drift = confidence_drift(summaries)
    assert drift.trend == "flat"
    assert drift.delta == 0.0


def test_confidence_drift_rejects_negative_flat_threshold():
    with pytest.raises(ValueError, match="flat_threshold"):
        confidence_drift([], flat_threshold=-0.01)


def test_confidence_drift_wire_dict_round_trips():
    summaries = [
        CalibrationSummary(0.7, 0.50, {}, 10),
        CalibrationSummary(0.7, 0.85, {}, 10),
    ]
    drift = confidence_drift(summaries)
    wire = drift.as_wire_dict()
    assert wire["trend"] == "improving"
    assert wire["delta"] == 0.35
    assert wire["n_summaries"] == 2


# ---------------------------------------------------------------------------
# modal_outcome_distribution
# ---------------------------------------------------------------------------


def test_modal_outcome_distribution_counts_actual_values():
    pred = _prediction()
    pairs = [
        pair_against_reality(pred, actual_outcome={"outcome": "accept"}),
        pair_against_reality(pred, actual_outcome={"outcome": "accept"}),
        pair_against_reality(pred, actual_outcome={"outcome": "reject"}),
        pair_against_reality(pred, actual_outcome={"outcome": "renegotiate"}),
    ]
    dist = modal_outcome_distribution(pairs, key="outcome")
    assert isinstance(dist, ModalOutcomeDistribution)
    assert dist.side == "actual"
    assert dist.n == 4
    assert dist.counts == {"accept": 2, "reject": 1, "renegotiate": 1}


def test_modal_outcome_distribution_counts_projected_side():
    pred_a = _prediction(projected={"outcome": "accept"})
    pred_r = _prediction(projected={"outcome": "reject"})
    pairs = [
        pair_against_reality(pred_a, actual_outcome={"outcome": "accept"}),
        pair_against_reality(pred_a, actual_outcome={"outcome": "reject"}),
        pair_against_reality(pred_r, actual_outcome={"outcome": "reject"}),
    ]
    dist = modal_outcome_distribution(pairs, key="outcome", side="projected")
    assert dist.counts == {"accept": 2, "reject": 1}


def test_modal_outcome_distribution_skips_pairs_missing_the_key():
    pred = _prediction()
    pairs = [
        pair_against_reality(pred, actual_outcome={"outcome": "accept"}),
        pair_against_reality(pred, actual_outcome={"amount": 100}),  # no outcome key
    ]
    dist = modal_outcome_distribution(pairs, key="outcome")
    assert dist.n == 1
    assert dist.counts == {"accept": 1}


def test_modal_outcome_distribution_wire_dict():
    pred = _prediction()
    pair = pair_against_reality(pred, actual_outcome={"outcome": "accept"})
    dist = modal_outcome_distribution([pair], key="outcome")
    wire = dist.as_wire_dict()
    assert wire == {
        "side": "actual",
        "key": "outcome",
        "counts": {"accept": 1},
        "n": 1,
    }


# ---------------------------------------------------------------------------
# index_predictions helper
# ---------------------------------------------------------------------------


def test_index_predictions_builds_id_to_record_map():
    preds = [_prediction() for _ in range(3)]
    lookup = index_predictions(preds)
    assert set(lookup.keys()) == {p.id for p in preds}
    assert lookup[preds[0].id] is preds[0]


# ---------------------------------------------------------------------------
# Cross-primitive: aggregator round-trips with calibration.aggregate_pairs
# ---------------------------------------------------------------------------


def test_summarize_by_one_bucket_matches_aggregate_pairs():
    """A single-key summarize_by should match calling aggregate_pairs
    directly on the whole pair list — sanity-check that grouping
    doesn't perturb the underlying math."""
    preds = [_prediction(projected={"outcome": "accept"}) for _ in range(4)]
    pairs = []
    for i, pred in enumerate(preds):
        actual = "accept" if i < 3 else "reject"
        pairs.append(pair_against_reality(pred, actual_outcome={"outcome": actual}))
    direct = aggregate_pairs(pairs, predictions_by_id=index_predictions(preds))
    grouped = summarize_by(
        pairs,
        predictions_by_id=index_predictions(preds),
        key_fn=lambda *_args: "all",
    )
    assert "all" in grouped
    assert grouped["all"].n_pairs == direct.n_pairs
    assert grouped["all"].modal_accuracy == direct.modal_accuracy
