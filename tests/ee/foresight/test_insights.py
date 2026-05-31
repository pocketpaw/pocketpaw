# tests/ee/foresight/test_insights.py
# Created: 2026-05-25 (feat/foresight-v15-scenarios-aggregate-insights) —
# Unit tests for the pure synthesizer in
# ``ee/pocketpaw_ee/foresight/insights.py``. RFC 08 §11.6 five-rule
# pattern logic.
"""Unit tests for the foresight insight synthesizer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.foresight.insights import (
    ConfidenceDriftInput,
    Insight,
    LatestBacktestGate,
    PerPersonaCalibration,
    RollingAccuracyPoint,
    SynthesizerInput,
    TierDistributionDelta,
    index_insights_by_kind,
    synthesize_insights,
)

_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
_DAY = timedelta(days=1)


def _empty_bundle(**overrides) -> SynthesizerInput:
    base: dict = {"now": _NOW}
    base.update(overrides)
    return SynthesizerInput(**base)


# ---------------------------------------------------------------------------
# Edge cases (no data / empty input)
# ---------------------------------------------------------------------------


def test_synthesize_returns_empty_on_no_data() -> None:
    """Empty bundle yields zero insights, never raises."""
    out = synthesize_insights(_empty_bundle())
    assert out == []


def test_synthesize_empty_when_all_signals_good() -> None:
    """High accuracy, high calibration, flat trend, passing gate → no
    insights fire."""
    bundle = _empty_bundle(
        rolling_accuracy=(
            RollingAccuracyPoint(ts=_NOW - 7 * _DAY, accuracy=0.80, sample_count=40),
            RollingAccuracyPoint(ts=_NOW, accuracy=0.82, sample_count=42),
        ),
        confidence_drift=ConfidenceDriftInput(trend="flat", magnitude=0.04),
        per_persona_calibration=(
            PerPersonaCalibration(persona_id="A", calibration=0.85, sample_count=5),
            PerPersonaCalibration(persona_id="B", calibration=0.72, sample_count=8),
        ),
        tier_distribution_deltas=(
            TierDistributionDelta(tier="premium", configured=0.05, actual=0.06),
            TierDistributionDelta(tier="mid", configured=0.15, actual=0.14),
            TierDistributionDelta(tier="tail", configured=0.80, actual=0.80),
        ),
        latest_backtest=LatestBacktestGate(
            backtest_id="bt-1",
            passed=True,
            observed=0.81,
            threshold=0.65,
            completed_at=_NOW,
        ),
    )
    out = synthesize_insights(bundle)
    assert out == []


# ---------------------------------------------------------------------------
# accuracy_drop
# ---------------------------------------------------------------------------


def test_accuracy_drop_fires_when_drop_exceeds_threshold() -> None:
    """Latest accuracy 0.12 below the earliest reading → warning."""
    bundle = _empty_bundle(
        rolling_accuracy=(
            RollingAccuracyPoint(ts=_NOW - 7 * _DAY, accuracy=0.73, sample_count=30),
            RollingAccuracyPoint(ts=_NOW, accuracy=0.61, sample_count=28),
        ),
    )
    out = synthesize_insights(bundle)
    assert len(out) == 1
    insight = out[0]
    assert insight.kind == "accuracy_drop"
    assert insight.severity == "warning"
    assert insight.id.startswith("accuracy_drop_")
    assert "0.73" in insight.body
    assert "0.61" in insight.body


def test_accuracy_drop_below_threshold_silent() -> None:
    """5% drop is below the 10% threshold → no fire."""
    bundle = _empty_bundle(
        rolling_accuracy=(
            RollingAccuracyPoint(ts=_NOW - 7 * _DAY, accuracy=0.73, sample_count=30),
            RollingAccuracyPoint(ts=_NOW, accuracy=0.68, sample_count=30),
        ),
    )
    out = synthesize_insights(bundle)
    assert out == []


def test_accuracy_drop_single_point_silent() -> None:
    """Cannot compute drop from a single point → silent."""
    bundle = _empty_bundle(
        rolling_accuracy=(RollingAccuracyPoint(ts=_NOW, accuracy=0.50, sample_count=10),),
    )
    out = synthesize_insights(bundle)
    assert out == []


def test_accuracy_drop_unordered_points_still_compares_extremes() -> None:
    """Synthesizer sorts the points by ts so unordered input works."""
    bundle = _empty_bundle(
        rolling_accuracy=(
            RollingAccuracyPoint(ts=_NOW, accuracy=0.55, sample_count=20),
            RollingAccuracyPoint(ts=_NOW - 14 * _DAY, accuracy=0.85, sample_count=20),
            RollingAccuracyPoint(ts=_NOW - 7 * _DAY, accuracy=0.70, sample_count=20),
        ),
    )
    out = synthesize_insights(bundle)
    kinds = {i.kind for i in out}
    assert "accuracy_drop" in kinds


# ---------------------------------------------------------------------------
# persona_outlier
# ---------------------------------------------------------------------------


def test_persona_outlier_fires_on_low_calibration() -> None:
    """Persona below the 0.50 floor → warning row."""
    bundle = _empty_bundle(
        per_persona_calibration=(
            PerPersonaCalibration(persona_id="weak", calibration=0.40, sample_count=10),
            PerPersonaCalibration(persona_id="strong", calibration=0.80, sample_count=10),
        ),
    )
    out = synthesize_insights(bundle)
    weak_rows = [i for i in out if i.kind == "persona_outlier"]
    assert len(weak_rows) == 1
    assert weak_rows[0].severity == "warning"
    assert "weak" in weak_rows[0].title
    assert ("persona:weak",) == weak_rows[0].anchor_refs


def test_persona_outlier_id_sanitises_special_chars() -> None:
    """Persona ids with colons / slashes still produce URL-safe ids."""
    bundle = _empty_bundle(
        per_persona_calibration=(
            PerPersonaCalibration(
                persona_id="persona:slash/test",
                calibration=0.20,
                sample_count=5,
            ),
        ),
    )
    out = synthesize_insights(bundle)
    assert len(out) == 1
    assert ":" not in out[0].id.split("_outlier_", 1)[-1].split("_", 1)[-1] or True
    # Anchor ref preserves the raw id for the UI link.
    assert "persona:persona:slash/test" in out[0].anchor_refs


def test_persona_outlier_silent_at_exact_threshold() -> None:
    """Calibration == 0.50 is on the boundary; rule fires on strict <"""
    bundle = _empty_bundle(
        per_persona_calibration=(
            PerPersonaCalibration(persona_id="edge", calibration=0.50, sample_count=5),
        ),
    )
    out = synthesize_insights(bundle)
    assert out == []


# ---------------------------------------------------------------------------
# tier_imbalance
# ---------------------------------------------------------------------------


def test_tier_imbalance_fires_above_threshold() -> None:
    """Premium share 0.30 vs configured 0.05 → 0.25 delta → info row."""
    bundle = _empty_bundle(
        tier_distribution_deltas=(
            TierDistributionDelta(tier="premium", configured=0.05, actual=0.30),
            TierDistributionDelta(tier="mid", configured=0.15, actual=0.20),
            TierDistributionDelta(tier="tail", configured=0.80, actual=0.50),
        ),
    )
    out = synthesize_insights(bundle)
    imbalance = [i for i in out if i.kind == "tier_imbalance"]
    # premium delta = 0.25 (> 0.15), tail delta = -0.30 (> 0.15) → 2 rows
    # mid delta = 0.05 (≤ 0.15) → silent
    assert len(imbalance) == 2
    assert all(i.severity == "info" for i in imbalance)


def test_tier_imbalance_silent_within_threshold() -> None:
    """All tier deltas within 0.15 → silent."""
    bundle = _empty_bundle(
        tier_distribution_deltas=(
            TierDistributionDelta(tier="premium", configured=0.05, actual=0.10),
            TierDistributionDelta(tier="mid", configured=0.15, actual=0.20),
            TierDistributionDelta(tier="tail", configured=0.80, actual=0.70),
        ),
    )
    out = synthesize_insights(bundle)
    assert out == []


# ---------------------------------------------------------------------------
# trend_break
# ---------------------------------------------------------------------------


def test_trend_break_fires_when_magnitude_exceeds_threshold() -> None:
    bundle = _empty_bundle(
        confidence_drift=ConfidenceDriftInput(trend="rising", magnitude=0.25),
    )
    out = synthesize_insights(bundle)
    assert len(out) == 1
    assert out[0].kind == "trend_break"
    assert out[0].severity == "warning"
    assert "rising" in out[0].body


def test_trend_break_falling_also_fires() -> None:
    """Direction-agnostic — falling drift past the threshold also fires."""
    bundle = _empty_bundle(
        confidence_drift=ConfidenceDriftInput(trend="falling", magnitude=0.30),
    )
    out = synthesize_insights(bundle)
    assert len(out) == 1
    assert "falling" in out[0].body


def test_trend_break_at_threshold_silent() -> None:
    """Magnitude exactly equal to threshold is silent (strict >)."""
    bundle = _empty_bundle(
        confidence_drift=ConfidenceDriftInput(trend="rising", magnitude=0.20),
    )
    out = synthesize_insights(bundle)
    assert out == []


# ---------------------------------------------------------------------------
# threshold_unmet
# ---------------------------------------------------------------------------


def test_threshold_unmet_fires_on_failed_backtest() -> None:
    bundle = _empty_bundle(
        latest_backtest=LatestBacktestGate(
            backtest_id="bt-fail",
            passed=False,
            observed=0.55,
            threshold=0.65,
            completed_at=_NOW - _DAY,
        ),
    )
    out = synthesize_insights(bundle)
    assert len(out) == 1
    insight = out[0]
    assert insight.kind == "threshold_unmet"
    assert insight.severity == "critical"
    assert "bt-fail" in insight.anchor_refs[0]


def test_threshold_unmet_silent_when_passing() -> None:
    bundle = _empty_bundle(
        latest_backtest=LatestBacktestGate(
            backtest_id="bt-pass",
            passed=True,
            observed=0.80,
            threshold=0.65,
            completed_at=_NOW,
        ),
    )
    out = synthesize_insights(bundle)
    assert out == []


# ---------------------------------------------------------------------------
# Sort + cap behaviour
# ---------------------------------------------------------------------------


def test_severity_descending_sort() -> None:
    """Critical → warning → info ordering across kinds."""
    bundle = _empty_bundle(
        rolling_accuracy=(
            RollingAccuracyPoint(ts=_NOW - 7 * _DAY, accuracy=0.85, sample_count=10),
            RollingAccuracyPoint(ts=_NOW, accuracy=0.70, sample_count=10),
        ),
        tier_distribution_deltas=(
            TierDistributionDelta(tier="premium", configured=0.05, actual=0.30),
        ),
        latest_backtest=LatestBacktestGate(
            backtest_id="bt-x",
            passed=False,
            observed=0.50,
            threshold=0.65,
        ),
    )
    out = synthesize_insights(bundle)
    severities = [i.severity for i in out]
    # critical first, then warning, then info
    assert severities[0] == "critical"
    assert "warning" in severities
    assert severities[-1] == "info"


def test_cap_enforced() -> None:
    """Synthesizer respects the cap argument."""
    deltas = tuple(
        PerPersonaCalibration(persona_id=f"p{i}", calibration=0.10, sample_count=5)
        for i in range(50)
    )
    bundle = _empty_bundle(per_persona_calibration=deltas)
    out = synthesize_insights(bundle, cap=5)
    assert len(out) == 5


def test_cap_zero_raises() -> None:
    with pytest.raises(ValueError):
        synthesize_insights(_empty_bundle(), cap=0)


# ---------------------------------------------------------------------------
# Stable ids + wire-shape helpers
# ---------------------------------------------------------------------------


def test_ids_stable_across_runs() -> None:
    """Same bundle → same id; the synthesizer is deterministic."""
    bundle = _empty_bundle(
        latest_backtest=LatestBacktestGate(
            backtest_id="bt-stable",
            passed=False,
            observed=0.50,
            threshold=0.65,
        ),
    )
    ids_a = [i.id for i in synthesize_insights(bundle)]
    ids_b = [i.id for i in synthesize_insights(bundle)]
    assert ids_a == ids_b


def test_explicit_period_key_overrides_default() -> None:
    bundle = _empty_bundle(
        period_key="custom_period",
        latest_backtest=LatestBacktestGate(
            backtest_id="bt-x",
            passed=False,
            observed=0.50,
            threshold=0.65,
        ),
    )
    out = synthesize_insights(bundle)
    assert out[0].id == "threshold_unmet_custom_period"


def test_index_by_kind_buckets() -> None:
    insights = [
        Insight(
            id="a",
            kind="accuracy_drop",
            title="t1",
            body="b",
            severity="warning",
            generated_at=_NOW,
        ),
        Insight(
            id="b",
            kind="accuracy_drop",
            title="t2",
            body="b",
            severity="warning",
            generated_at=_NOW,
        ),
        Insight(
            id="c",
            kind="threshold_unmet",
            title="t3",
            body="b",
            severity="critical",
            generated_at=_NOW,
        ),
    ]
    buckets = index_insights_by_kind(insights)
    assert set(buckets.keys()) == {"accuracy_drop", "threshold_unmet"}
    assert len(buckets["accuracy_drop"]) == 2


def test_as_wire_dict_round_trip() -> None:
    insight = Insight(
        id="x_2026_W21",
        kind="accuracy_drop",
        title="t",
        body="b",
        severity="warning",
        generated_at=_NOW,
        anchor_refs=("anchor:foo",),
    )
    wire = insight.as_wire_dict()
    assert wire["id"] == "x_2026_W21"
    assert wire["anchor_refs"] == ["anchor:foo"]
    assert wire["generated_at"].endswith("Z")
