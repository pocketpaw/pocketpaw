# tests/ee/foresight/test_calibration.py
# Created: 2026-05-25 (feat/foresight-v03-calibration) — RFC 08 PR 3.
#
# Pin the calibration loop contract (RFC §9):
#   - PredictionRecord round-trips through as_wire_dict.
#   - PredictionBuffer captures + retrieves pending predictions.
#   - find_by_anchor returns only unmatched predictions.
#   - mark_observed gates a prediction out of pending.
#   - pair_against_reality computes numeric / string / missing deltas.
#   - aggregate_pairs computes modal_accuracy, per_metric_accuracy,
#     confidence_calibration.
#   - apply_correction caps raw deltas to ±10% per cycle (RFC gate 6).
#   - apply_correction rejects unknown layers + non-positive caps.
#   - build_prediction_record validates confidence range.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pocketpaw_ee.foresight.calibration import (
    CORRECTION_CAP,
    PredictionBuffer,
    PredictionRecord,
    aggregate_pairs,
    apply_correction,
    build_prediction_record,
    pair_against_reality,
)

# --- PredictionRecord -----------------------------------------------


def _sample_prediction(
    *,
    anchor: str = "lease:LR-2026-117",
    projected: dict | None = None,
    confidence: float = 0.78,
    observe_offset_seconds: int = 0,
) -> PredictionRecord:
    return build_prediction_record(
        scenario_template="decision_forecast.yaml",
        run_id=uuid4(),
        anchor_object_id=anchor,
        projected_outcome=projected or {"outcome": "accept", "amount": 2850.0},
        observe_at=datetime.now(UTC) + timedelta(seconds=observe_offset_seconds),
        projection_confidence=confidence,
    )


def test_prediction_record_round_trips_through_wire_dict():
    record = _sample_prediction()
    wire = record.as_wire_dict()
    assert wire["scenario_template"] == "decision_forecast.yaml"
    assert wire["anchor_object_id"] == "lease:LR-2026-117"
    assert wire["projected_outcome"] == {"outcome": "accept", "amount": 2850.0}
    assert wire["projection_confidence"] == 0.78


def test_build_prediction_record_rejects_out_of_band_confidence():
    with pytest.raises(ValueError, match="must be in"):
        build_prediction_record(
            scenario_template="x.yaml",
            run_id=uuid4(),
            anchor_object_id="x",
            projected_outcome={},
            observe_at=datetime.now(UTC),
            projection_confidence=1.5,  # out of [0.0, 1.0]
        )


# --- PredictionBuffer -----------------------------------------------


async def test_buffer_capture_and_pending_round_trip():
    buf = PredictionBuffer()
    record = _sample_prediction(observe_offset_seconds=-1)  # already due
    await buf.capture(record)
    pending = await buf.pending()
    assert len(pending) == 1
    assert pending[0].id == record.id


async def test_buffer_pending_filters_by_observe_at():
    buf = PredictionBuffer()
    past = _sample_prediction(observe_offset_seconds=-3600)
    future = _sample_prediction(observe_offset_seconds=3600)
    await buf.capture(past)
    await buf.capture(future)
    pending = await buf.pending()
    # Only the past prediction is due.
    assert {p.id for p in pending} == {past.id}


async def test_buffer_find_by_anchor_filters_unmatched():
    buf = PredictionBuffer()
    a = _sample_prediction(anchor="lease:A")
    b = _sample_prediction(anchor="lease:B")
    c = _sample_prediction(anchor="lease:A")
    await buf.capture(a)
    await buf.capture(b)
    await buf.capture(c)
    hits = await buf.find_by_anchor("lease:A")
    assert {h.id for h in hits} == {a.id, c.id}


async def test_buffer_mark_observed_removes_from_pending():
    buf = PredictionBuffer()
    record = _sample_prediction(observe_offset_seconds=-1)
    await buf.capture(record)
    await buf.mark_observed(record.id, {"outcome": "accept", "amount": 2880.0})
    pending = await buf.pending()
    assert pending == []
    assert buf.observed_count == 1


async def test_buffer_mark_observed_raises_on_unknown_id():
    buf = PredictionBuffer()
    with pytest.raises(KeyError):
        await buf.mark_observed(uuid4(), {})


# --- pair_against_reality -------------------------------------------


def test_pair_computes_numeric_delta():
    prediction = _sample_prediction(projected={"amount": 2850.0, "rent": 1200.0})
    pair = pair_against_reality(
        prediction,
        actual_outcome={"amount": 2880.0, "rent": 1180.0},
    )
    assert pair.delta["amount"] == pytest.approx(30.0)
    assert pair.delta["rent"] == pytest.approx(-20.0)


def test_pair_computes_string_match():
    prediction = _sample_prediction(projected={"outcome": "accept"})
    pair = pair_against_reality(prediction, actual_outcome={"outcome": "accept"})
    assert pair.delta["outcome"]["match"] is True


def test_pair_computes_string_mismatch():
    prediction = _sample_prediction(projected={"outcome": "accept"})
    pair = pair_against_reality(prediction, actual_outcome={"outcome": "reject"})
    assert pair.delta["outcome"]["match"] is False
    assert pair.delta["outcome"]["projected"] == "accept"
    assert pair.delta["outcome"]["actual"] == "reject"


def test_pair_marks_missing_keys_in_either_side():
    prediction = _sample_prediction(projected={"a": 1, "b": 2})
    pair = pair_against_reality(prediction, actual_outcome={"b": 2, "c": 3})
    assert pair.delta["a"] == {"missing_in": "actual"}
    assert pair.delta["c"] == {"missing_in": "projected"}


def test_pair_wire_dict_serializes_uuids_and_datetime():
    prediction = _sample_prediction()
    pair = pair_against_reality(prediction, actual_outcome={"outcome": "accept"})
    wire = pair.as_wire_dict()
    assert isinstance(wire["prediction_id"], str)
    assert "paired_at" in wire and isinstance(wire["paired_at"], str)


# --- aggregate_pairs ------------------------------------------------


def test_aggregate_empty_returns_zero_stats():
    summary = aggregate_pairs([])
    assert summary.n_pairs == 0
    assert summary.modal_accuracy == 0.0


def test_aggregate_modal_accuracy_with_matches():
    """3 pairs, all matching → modal_accuracy 1.0."""
    pairs = []
    for _ in range(3):
        record = _sample_prediction(projected={"outcome": "accept"})
        pairs.append(pair_against_reality(record, actual_outcome={"outcome": "accept"}))
    summary = aggregate_pairs(pairs)
    assert summary.modal_accuracy == pytest.approx(1.0)
    assert summary.n_pairs == 3


def test_aggregate_modal_accuracy_with_mismatches():
    """2 matching + 1 mismatch → modal_accuracy 2/3."""
    pairs = []
    for outcome_actual in ["accept", "accept", "reject"]:
        record = _sample_prediction(projected={"outcome": "accept"})
        pairs.append(pair_against_reality(record, actual_outcome={"outcome": outcome_actual}))
    summary = aggregate_pairs(pairs)
    assert summary.modal_accuracy == pytest.approx(2 / 3)


def test_aggregate_per_metric_breakdown():
    """Outcome matches 2/3, amount matches 1/3 → per-metric split."""
    pairs = []
    rows = [
        ("accept", 2850.0),  # matches
        ("accept", 3200.0),  # outcome match, amount mismatch
        ("reject", 2900.0),  # outcome mismatch, amount mismatch
    ]
    for outcome, amount in rows:
        record = _sample_prediction(projected={"outcome": "accept", "amount": 2850.0})
        pairs.append(
            pair_against_reality(record, actual_outcome={"outcome": outcome, "amount": amount})
        )
    summary = aggregate_pairs(pairs, numeric_tolerance=0.05)  # tighter band
    assert summary.per_metric_accuracy["outcome"] == pytest.approx(2 / 3)
    # amount deltas: [0 (match), 350 (mismatch), 50 (mismatch)].
    # Tolerance is absolute (|delta| <= numeric_tolerance), so both 350
    # and 50 exceed 0.05. Only amount[0] matches → 1/3.
    assert summary.per_metric_accuracy["amount"] == pytest.approx(1 / 3)


def test_aggregate_confidence_calibration_with_perfect_calibration():
    """All predictions at confidence=0.7 and 70% land → calibration ~1.0."""
    pairs = []
    predictions_by_id = {}
    for i in range(10):
        record = _sample_prediction(projected={"outcome": "accept"}, confidence=0.7)
        predictions_by_id[record.id] = record
        # 7 of 10 match
        actual = "accept" if i < 7 else "reject"
        pairs.append(pair_against_reality(record, actual_outcome={"outcome": actual}))
    summary = aggregate_pairs(pairs, predictions_by_id=predictions_by_id)
    # modal_accuracy = 7/10 = 0.7; mean_conf = 0.7 → diff 0 → calibration 1.0
    assert summary.confidence_calibration == pytest.approx(1.0)


# --- apply_correction (the ±10% cap, RFC gate 6) --------------------


def test_correction_caps_positive_delta_at_default():
    correction = apply_correction(
        layer="persona_prior",
        target="conscientious_approver.openness",
        raw_delta=0.50,  # way over 0.10
        rationale="forecast over-rejected",
    )
    assert correction.capped_delta == pytest.approx(CORRECTION_CAP)
    assert correction.capped_delta == 0.10
    assert correction.raw_delta == 0.50  # raw preserved


def test_correction_caps_negative_delta_at_default():
    correction = apply_correction(
        layer="action_propensity",
        target="accept_under_compset",
        raw_delta=-0.30,
        rationale="over-accepted",
    )
    assert correction.capped_delta == pytest.approx(-CORRECTION_CAP)


def test_correction_passes_through_small_delta():
    correction = apply_correction(
        layer="aggregator_weight",
        target="throughput_vs_queue",
        raw_delta=0.03,
        rationale="minor nudge",
    )
    assert correction.capped_delta == pytest.approx(0.03)


def test_correction_default_is_auto():
    correction = apply_correction(
        layer="persona_prior",
        target="x",
        raw_delta=0.01,
        rationale="",
    )
    assert correction.auto is True


def test_correction_can_be_marked_for_captain_review():
    """DESIGN TENSION 1 — flag for captain review when needed."""
    correction = apply_correction(
        layer="persona_prior",
        target="x",
        raw_delta=0.20,
        rationale="material shift; needs human review",
        auto=False,
    )
    assert correction.auto is False
    assert correction.capped_delta == pytest.approx(CORRECTION_CAP)


def test_correction_rejects_unknown_layer():
    with pytest.raises(ValueError, match="unknown correction layer"):
        apply_correction(
            layer="not_a_real_layer",
            target="x",
            raw_delta=0.01,
            rationale="",
        )


def test_correction_rejects_zero_or_negative_cap():
    with pytest.raises(ValueError, match="cap must be > 0"):
        apply_correction(
            layer="persona_prior",
            target="x",
            raw_delta=0.01,
            rationale="",
            cap=0,
        )


def test_correction_honors_custom_cap():
    correction = apply_correction(
        layer="persona_prior",
        target="x",
        raw_delta=0.30,
        rationale="",
        cap=0.05,
    )
    assert correction.capped_delta == pytest.approx(0.05)


def test_correction_wire_dict_serializes():
    correction = apply_correction(
        layer="persona_prior",
        target="x",
        raw_delta=0.30,
        rationale="why",
    )
    wire = correction.as_wire_dict()
    assert wire["layer"] == "persona_prior"
    assert wire["target"] == "x"
    assert wire["capped_delta"] == 0.10
    assert wire["raw_delta"] == 0.30
    assert wire["auto"] is True


def test_correction_cap_constant_is_exposed():
    """Tests should be able to introspect the locked cap."""
    assert CORRECTION_CAP == 0.10
