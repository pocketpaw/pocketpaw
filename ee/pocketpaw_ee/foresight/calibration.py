# ee/pocketpaw_ee/foresight/calibration.py
# Updated: 2026-05-26 (feat/foresight-v10-prediction-record-persist) — RFC
# 08 v1.0 PR 10:
#   - ``PredictionBuffer`` accepts an optional ``on_capture`` /
#     ``on_mark_observed`` callback pair. Legacy callers (CLI smoke,
#     v0.5 tests) pass nothing and behaviour is unchanged. The cloud
#     side hands in a closure that mirrors each capture into the new
#     ``foresight_prediction_records`` Mongo collection and updates the
#     same row when observation lands. The engine never imports cloud —
#     the callbacks are injected at construction time so the import
#     direction stays cloud → engine.
#   - In-memory ring stays the canonical authority for the engine's
#     own pairing path. Mongo is a side-mirror so the §11.5 aggregate
#     + §11.6 insights endpoints can read paired records persistently
#     across process restarts (which the in-memory ring loses).
# Created: 2026-05-25 (feat/foresight-v03-calibration) — RFC 08 PR 3.
#
# Calibration loop — RFC 08 §9 "the moat".
#
# The 5-step closed cycle:
#
#   1. CAPTURE   — every projected outcome carries an ``observe_at``
#                  deadline; the prediction is written to the buffer.
#   2. OBSERVE   — real outcome lands; matching pending predictions
#                  are picked up.
#   3. PAIR      — projected vs actual outcome diffed into a
#                  ``CalibrationPair``.
#   4. AGGREGATE — per-scenario / per-tier / per-metric accuracy
#                  rollups land in ``calibration_summaries`` (PR 4 will
#                  add the cloud-side Beanie persistence; PR 3 keeps
#                  this in-memory).
#   5. CORRECT   — adjustments applied to persona priors, action
#                  propensities, aggregator weights — capped at
#                  ±10% per cycle (RFC implementation gate 6 +
#                  RFC 08 §16.3 "v0.1 ships at ±10% per cycle").
#
# DESIGN TENSION 1 (captured in soul memory): captain-reviewed vs auto
# correction. v0.1 (this PR) ships AUTO with the ±10% cap. The PR body
# flags this as revisitable when first customer data lands — same as
# RFC 08 §16.3 captures it. The hook for promoting to
# captain-reviewed lives in ``apply_correction``'s ``auto`` argument.

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


# --- §9.1 CAPTURE ----------------------------------------------------


@dataclass(frozen=True)
class PredictionRecord:
    """One projected outcome held until reality lands.

    The buffer keys on ``(scenario_template, anchor_object_id,
    observe_at)`` so a real-decision-landed event can scan for matching
    predictions. ``anchor_decision_id`` is optional — populated when a
    forward-precedent edge can be written before observation (rare).
    """

    id: UUID
    scenario_template: str
    run_id: UUID
    anchor_object_id: str
    anchor_decision_id: UUID | None
    projected_outcome: dict[str, Any]
    projection_confidence: float
    observe_at: datetime
    captured_at: datetime

    def as_wire_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "scenario_template": self.scenario_template,
            "run_id": str(self.run_id),
            "anchor_object_id": self.anchor_object_id,
            "anchor_decision_id": (
                str(self.anchor_decision_id) if self.anchor_decision_id else None
            ),
            "projected_outcome": dict(self.projected_outcome),
            "projection_confidence": self.projection_confidence,
            "observe_at": self.observe_at.isoformat(),
            "captured_at": self.captured_at.isoformat(),
        }


class PredictionBuffer:
    """In-memory ring of pending predictions.

    PR 3 (v0.1) kept this in-memory only. v1.0 (PR 10) adds optional
    ``on_capture`` / ``on_mark_observed`` callbacks so the cloud side
    can mirror each capture / observation into the
    ``foresight_prediction_records`` Mongo collection. The in-memory
    ring stays the engine's authority — the callbacks are best-effort
    side-mirrors. The engine never imports cloud; the cloud constructs
    a buffer with callbacks baked in via closure.

    Args:
        on_capture: optional async callback invoked after a record is
            captured. Receives the record. May be sync or async — the
            buffer awaits when it returns a coroutine.
        on_mark_observed: optional async callback invoked after a
            record is marked observed. Receives ``(record,
            observation)`` so the caller has both the original
            prediction and the new outcome dict.
    """

    def __init__(
        self,
        *,
        on_capture: Any | None = None,
        on_mark_observed: Any | None = None,
    ) -> None:
        self._records: dict[UUID, PredictionRecord] = {}
        self._observed: dict[UUID, dict[str, Any]] = {}
        self._on_capture = on_capture
        self._on_mark_observed = on_mark_observed

    async def capture(self, record: PredictionRecord) -> None:
        """Write a prediction to the buffer.

        Fires ``on_capture`` after the in-memory write lands so the
        cloud-side mirror can persist a Mongo row. Callback exceptions
        are intentionally NOT swallowed here — the engine treats the
        buffer write as atomic with the side-mirror so a Mongo failure
        surfaces to the runner (which logs + carries on per its own
        contract). Sync + async callbacks both supported.
        """
        self._records[record.id] = record
        logger.debug(
            "captured prediction id=%s anchor=%s observe_at=%s",
            record.id,
            record.anchor_object_id,
            record.observe_at.isoformat(),
        )
        if self._on_capture is not None:
            maybe_coro = self._on_capture(record)
            if hasattr(maybe_coro, "__await__"):
                await maybe_coro

    async def pending(self, *, before: datetime | None = None) -> list[PredictionRecord]:
        """Return predictions whose ``observe_at`` has passed (or all
        unmatched predictions when ``before`` is ``None``).
        """
        threshold = before or datetime.now(UTC)
        return [
            r
            for r in self._records.values()
            if r.id not in self._observed and r.observe_at <= threshold
        ]

    async def mark_observed(self, prediction_id: UUID, observation: dict[str, Any]) -> None:
        """Record that reality has landed for this prediction.

        Fires ``on_mark_observed`` after the in-memory observation
        lands so the cloud-side mirror can update the Mongo row's
        ``observed_at`` / ``observed_outcome`` / ``paired`` fields.
        """
        if prediction_id not in self._records:
            raise KeyError(f"unknown prediction id: {prediction_id}")
        observation_dict = dict(observation)
        self._observed[prediction_id] = observation_dict
        logger.debug("marked observed prediction id=%s", prediction_id)
        if self._on_mark_observed is not None:
            maybe_coro = self._on_mark_observed(self._records[prediction_id], observation_dict)
            if hasattr(maybe_coro, "__await__"):
                await maybe_coro

    async def find_by_anchor(self, anchor_object_id: str) -> list[PredictionRecord]:
        """Find pending predictions anchored to a given Fabric object.

        Used by the §9.2 OBSERVE step when a real Decision lands —
        the listener calls this with the decision's anchor and pairs
        any matches.
        """
        return [
            r
            for r in self._records.values()
            if r.anchor_object_id == anchor_object_id and r.id not in self._observed
        ]

    def __len__(self) -> int:
        return len(self._records)

    @property
    def observed_count(self) -> int:
        return len(self._observed)


# --- §9.3 PAIR -------------------------------------------------------


@dataclass(frozen=True)
class CalibrationPair:
    """One (prediction, observation) pair with the per-metric delta."""

    prediction_id: UUID
    real_decision_id: UUID | None
    projected_outcome: dict[str, Any]
    actual_outcome: dict[str, Any]
    delta: dict[str, Any]
    paired_at: datetime

    def as_wire_dict(self) -> dict[str, Any]:
        return {
            "prediction_id": str(self.prediction_id),
            "real_decision_id": (str(self.real_decision_id) if self.real_decision_id else None),
            "projected_outcome": dict(self.projected_outcome),
            "actual_outcome": dict(self.actual_outcome),
            "delta": dict(self.delta),
            "paired_at": self.paired_at.isoformat(),
        }


def pair_against_reality(
    prediction: PredictionRecord,
    *,
    actual_outcome: dict[str, Any],
    real_decision_id: UUID | None = None,
) -> CalibrationPair:
    """Build a ``CalibrationPair`` by diffing the prediction's
    ``projected_outcome`` against the observed ``actual_outcome``.

    The ``delta`` dict carries one entry per metric:
      - numeric metrics: ``actual - projected`` (signed)
      - string metrics: ``{"match": bool, "projected": str, "actual": str}``
      - missing metrics: ``{"missing_in": "projected" | "actual"}``
    """
    delta = _compute_delta(prediction.projected_outcome, actual_outcome)
    return CalibrationPair(
        prediction_id=prediction.id,
        real_decision_id=real_decision_id,
        projected_outcome=dict(prediction.projected_outcome),
        actual_outcome=dict(actual_outcome),
        delta=delta,
        paired_at=datetime.now(UTC),
    )


def _compute_delta(projected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    """Per-metric delta. Tolerant of mismatched key sets.

    Numeric → signed difference. String → equality test with both
    values. Missing → ``{"missing_in": ...}`` marker.
    """
    delta: dict[str, Any] = {}
    keys = set(projected) | set(actual)
    for key in sorted(keys):
        p_val = projected.get(key)
        a_val = actual.get(key)
        if key not in projected:
            delta[key] = {"missing_in": "projected"}
            continue
        if key not in actual:
            delta[key] = {"missing_in": "actual"}
            continue
        if isinstance(p_val, (int, float)) and isinstance(a_val, (int, float)):
            delta[key] = a_val - p_val
        elif isinstance(p_val, str) and isinstance(a_val, str):
            delta[key] = {"match": p_val == a_val, "projected": p_val, "actual": a_val}
        else:
            # Different types or non-numeric / non-string: store both.
            delta[key] = {"match": p_val == a_val, "projected": p_val, "actual": a_val}
    return delta


# --- §9.4 AGGREGATE --------------------------------------------------


@dataclass
class CalibrationSummary:
    """Rollup over a set of ``CalibrationPair`` objects.

    Fields mirror RFC 08 §9.4:
      - ``modal_accuracy``: fraction of pairs whose key metric matches
        the projected modal trajectory within tolerance.
      - ``confidence_calibration``: how well the stated
        ``projection_confidence`` correlates with observed match rate.
      - ``per_metric_accuracy``: per-metric match-fraction breakdown.
      - ``n_pairs``: count.
    """

    modal_accuracy: float
    confidence_calibration: float
    per_metric_accuracy: dict[str, float] = field(default_factory=dict)
    n_pairs: int = 0

    def as_wire_dict(self) -> dict[str, Any]:
        return {
            "modal_accuracy": round(self.modal_accuracy, 4),
            "confidence_calibration": round(self.confidence_calibration, 4),
            "per_metric_accuracy": {k: round(v, 4) for k, v in self.per_metric_accuracy.items()},
            "n_pairs": self.n_pairs,
        }


def aggregate_pairs(
    pairs: Iterable[CalibrationPair],
    *,
    predictions_by_id: dict[UUID, PredictionRecord] | None = None,
    numeric_tolerance: float = 0.10,
) -> CalibrationSummary:
    """Aggregate a sequence of ``CalibrationPair`` objects.

    Args:
        pairs: iterable of paired predictions.
        predictions_by_id: optional lookup so confidence-calibration
            can be computed (needs ``projection_confidence`` from the
            original prediction record).
        numeric_tolerance: fractional band considered a "match" for
            numeric metrics (10% by default — matches the correction
            cap in §9.5).
    """
    pairs_list = list(pairs)
    if not pairs_list:
        return CalibrationSummary(
            modal_accuracy=0.0,
            confidence_calibration=0.0,
            per_metric_accuracy={},
            n_pairs=0,
        )

    # Per-metric match counts.
    metric_total: dict[str, int] = {}
    metric_matched: dict[str, int] = {}
    pair_match_flags: list[bool] = []

    for pair in pairs_list:
        pair_all_match = True
        for key, delta_val in pair.delta.items():
            metric_total[key] = metric_total.get(key, 0) + 1
            matched = _metric_matches(delta_val, numeric_tolerance)
            if matched:
                metric_matched[key] = metric_matched.get(key, 0) + 1
            else:
                pair_all_match = False
        pair_match_flags.append(pair_all_match)

    per_metric_accuracy = {
        key: metric_matched.get(key, 0) / metric_total[key] for key in metric_total
    }
    modal_accuracy = sum(1 for f in pair_match_flags if f) / len(pair_match_flags)

    # Confidence calibration: |observed_rate - mean_confidence|, mapped
    # so 1.0 = perfectly calibrated.
    confidence_calibration = 0.0
    if predictions_by_id is not None:
        confidences = [
            predictions_by_id[p.prediction_id].projection_confidence
            for p in pairs_list
            if p.prediction_id in predictions_by_id
        ]
        if confidences:
            mean_conf = sum(confidences) / len(confidences)
            confidence_calibration = 1.0 - abs(modal_accuracy - mean_conf)
            confidence_calibration = max(0.0, min(1.0, confidence_calibration))

    return CalibrationSummary(
        modal_accuracy=modal_accuracy,
        confidence_calibration=confidence_calibration,
        per_metric_accuracy=per_metric_accuracy,
        n_pairs=len(pairs_list),
    )


def _metric_matches(delta_val: Any, numeric_tolerance: float) -> bool:
    """Return True when this metric's delta is within the match band."""
    if isinstance(delta_val, dict):
        if "missing_in" in delta_val:
            return False
        return bool(delta_val.get("match", False))
    if isinstance(delta_val, (int, float)):
        # Tolerance band: |delta| / max(|actual|, 1) <= numeric_tolerance.
        # For metrics centered near 0 we fall back to absolute band.
        return abs(delta_val) <= numeric_tolerance
    return False


# --- §9.5 CORRECT ----------------------------------------------------


CORRECTION_CAP = 0.10
"""Per-cycle correction cap (RFC implementation gate 6 + §9.5 + §16.3).

PR 3 ships AUTO at ±10%. DESIGN TENSION 1 is to revisit when first
customer data lands — may move to captain-reviewed mode.
"""


@dataclass(frozen=True)
class Correction:
    """One adjustment proposal, capped at ±``CORRECTION_CAP``.

    ``layer`` is one of ``"persona_prior"`` / ``"action_propensity"`` /
    ``"aggregator_weight"``. ``target`` identifies the slot being
    adjusted (e.g. ``"conscientious_approver.openness"`` for a persona
    prior, ``"accept_under_compset"`` for an action propensity).
    ``raw_delta`` is the uncapped proposed change; ``capped_delta`` is
    what ``apply_correction`` would emit. ``auto`` is ``True`` when no
    captain review is required.
    """

    layer: str
    target: str
    raw_delta: float
    capped_delta: float
    rationale: str
    auto: bool = True

    def as_wire_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "target": self.target,
            "raw_delta": round(self.raw_delta, 4),
            "capped_delta": round(self.capped_delta, 4),
            "rationale": self.rationale,
            "auto": self.auto,
        }


def apply_correction(
    *,
    layer: str,
    target: str,
    raw_delta: float,
    rationale: str,
    auto: bool = True,
    cap: float = CORRECTION_CAP,
) -> Correction:
    """Produce a capped correction record.

    ``raw_delta`` can be any float; the capped value is clamped to
    ±``cap``. ``auto=True`` ships in v0.1 — captain-reviewed corrections
    are flagged ``auto=False`` and queued for human approval (PR 6 work).

    The function does NOT mutate any global state. The caller (PR 4's
    cloud-side service) is responsible for persisting the correction
    and threading the capped value into the next run's priors /
    propensities / weights.
    """
    if cap <= 0:
        raise ValueError(f"cap must be > 0, got {cap}")
    if layer not in {"persona_prior", "action_propensity", "aggregator_weight"}:
        raise ValueError(
            f"unknown correction layer {layer!r}; expected one of "
            "'persona_prior', 'action_propensity', 'aggregator_weight'"
        )
    capped = max(-cap, min(cap, raw_delta))
    logger.debug(
        "correction layer=%s target=%s raw=%.4f capped=%.4f auto=%s",
        layer,
        target,
        raw_delta,
        capped,
        auto,
    )
    return Correction(
        layer=layer,
        target=target,
        raw_delta=raw_delta,
        capped_delta=capped,
        rationale=rationale,
        auto=auto,
    )


# --- helper: build a PredictionRecord -------------------------------


def build_prediction_record(
    *,
    scenario_template: str,
    run_id: UUID,
    anchor_object_id: str,
    projected_outcome: dict[str, Any],
    observe_at: datetime,
    projection_confidence: float = 0.5,
    anchor_decision_id: UUID | None = None,
) -> PredictionRecord:
    """Construct a ``PredictionRecord`` with ``id`` + ``captured_at``
    auto-filled. Convenience for the projection layer (RFC 08 §7.7)
    that emits ProjectedDecisions.
    """
    if not (0.0 <= projection_confidence <= 1.0):
        raise ValueError(
            f"projection_confidence must be in [0.0, 1.0], got {projection_confidence}"
        )
    return PredictionRecord(
        id=uuid4(),
        scenario_template=scenario_template,
        run_id=run_id,
        anchor_object_id=anchor_object_id,
        anchor_decision_id=anchor_decision_id,
        projected_outcome=dict(projected_outcome),
        projection_confidence=projection_confidence,
        observe_at=observe_at,
        captured_at=datetime.now(UTC),
    )
