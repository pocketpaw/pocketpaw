# ee/pocketpaw_ee/foresight/aggregator.py
# Created: 2026-05-25 (feat/foresight-v04-backtest-aggregator) — RFC 08 PR 4.
#
# Aggregator primitives — RFC 08 §9.4 / §11.4 "Aggregate panel".
#
# Pure functions that roll CalibrationPair / CalibrationSummary /
# PredictionRecord shapes into higher-order views: per-group accuracy,
# rolling time-windowed accuracy, confidence drift, modal-outcome
# distributions, and threshold gating.
#
# These primitives have no I/O and no async — they're the same shape
# PR 3's calibration.py uses. The cloud-side backtest gate (this same
# PR) imports them lazily to compute the unlock criterion; the future
# UI Aggregate panel will call them via a thin cloud endpoint to render
# rolling metrics; the offline ops team will call them from a notebook
# to pressure-test customer accuracy.
#
# Design choices:
#
# 1. Pure functions only. Every function takes a list / dict / iterable
#    of values that PR 3's calibration loop produces, plus optional
#    keyword config (windows, group keys, thresholds), and returns a
#    plain dict / dataclass / scalar. No globals, no clock reads
#    (callers pass ``now`` for time-windowed primitives so tests stay
#    deterministic), no Beanie / FastAPI / pydantic.
#
# 2. Grouping is callable-driven. ``key_fn(record, pair) -> str`` lets
#    callers project any group axis they need — persona, sub-type,
#    scenario_template, anchor namespace, tier — without baking the
#    list into this module. Concrete helpers
#    (``per_scenario_template_summary``, ``per_anchor_namespace_summary``)
#    ship for the obvious axes; everything else uses ``summarize_by``
#    with a custom key_fn.
#
# 3. Reuse calibration.aggregate_pairs for the actual rollup math.
#    The aggregator doesn't redefine "accuracy" — it just slices the
#    pairs by group, time window, or threshold, then hands each slice
#    to the existing aggregator and returns the resulting summaries.

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pocketpaw_ee.foresight.calibration import (
    CalibrationPair,
    CalibrationSummary,
    PredictionRecord,
    aggregate_pairs,
)

GroupKeyFn = Callable[[PredictionRecord | None, CalibrationPair], str]


# ---------------------------------------------------------------------------
# Threshold gating
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdDecision:
    """The unlock-or-recalibrate verdict for one CalibrationSummary.

    ``passed`` is the boolean the onboarding gate uses; ``observed`` is
    the modal accuracy that was compared; ``threshold`` echoes the gate
    threshold so the UI can render "you scored 0.61 against the 0.65
    bar". ``margin`` is ``observed - threshold`` (negative when failing).
    """

    passed: bool
    observed: float
    threshold: float
    margin: float
    n_pairs: int

    def as_wire_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "observed": round(self.observed, 4),
            "threshold": round(self.threshold, 4),
            "margin": round(self.margin, 4),
            "n_pairs": self.n_pairs,
        }


def accuracy_meets_threshold(
    summary: CalibrationSummary,
    threshold: float,
    *,
    min_pairs: int = 1,
) -> ThresholdDecision:
    """Return a ThresholdDecision comparing ``summary.modal_accuracy`` to
    ``threshold``.

    ``min_pairs`` guards against thin samples — a summary with ``n_pairs``
    below the floor never passes (the customer needs more backtest
    anchors before the gate can fairly unlock).
    """
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold must be in [0.0, 1.0], got {threshold}")
    if min_pairs < 1:
        raise ValueError(f"min_pairs must be >= 1, got {min_pairs}")
    observed = summary.modal_accuracy
    sufficient = summary.n_pairs >= min_pairs
    passed = sufficient and observed >= threshold
    return ThresholdDecision(
        passed=passed,
        observed=observed,
        threshold=threshold,
        margin=observed - threshold,
        n_pairs=summary.n_pairs,
    )


# ---------------------------------------------------------------------------
# Grouping + per-group summaries
# ---------------------------------------------------------------------------


def group_pairs_by(
    pairs: Iterable[CalibrationPair],
    *,
    predictions_by_id: dict[UUID, PredictionRecord] | None = None,
    key_fn: GroupKeyFn,
) -> dict[str, list[CalibrationPair]]:
    """Split ``pairs`` into buckets keyed by ``key_fn(record, pair)``.

    When ``predictions_by_id`` is provided, ``key_fn`` receives the full
    ``PredictionRecord`` (or ``None`` when the record isn't in the map
    — typically because it was purged after observation). When the map
    is omitted, ``key_fn`` always receives ``None`` for the record arg
    and groups can only use information visible on the pair itself.
    """
    grouped: dict[str, list[CalibrationPair]] = {}
    by_id = predictions_by_id or {}
    for pair in pairs:
        record = by_id.get(pair.prediction_id)
        key = key_fn(record, pair)
        grouped.setdefault(key, []).append(pair)
    return grouped


def summarize_by(
    pairs: Iterable[CalibrationPair],
    *,
    predictions_by_id: dict[UUID, PredictionRecord] | None = None,
    key_fn: GroupKeyFn,
    numeric_tolerance: float = 0.10,
) -> dict[str, CalibrationSummary]:
    """Group ``pairs`` then aggregate each bucket via
    :func:`calibration.aggregate_pairs`. Returns ``dict[key, summary]``.

    The buckets share the ``numeric_tolerance`` band so per-group
    accuracy is comparable. Callers that need different bands per group
    (rare) call ``group_pairs_by`` + ``aggregate_pairs`` directly.
    """
    grouped = group_pairs_by(
        pairs,
        predictions_by_id=predictions_by_id,
        key_fn=key_fn,
    )
    return {
        key: aggregate_pairs(
            bucket,
            predictions_by_id=predictions_by_id,
            numeric_tolerance=numeric_tolerance,
        )
        for key, bucket in grouped.items()
    }


def per_scenario_template_summary(
    pairs: Iterable[CalibrationPair],
    *,
    predictions_by_id: dict[UUID, PredictionRecord],
    numeric_tolerance: float = 0.10,
) -> dict[str, CalibrationSummary]:
    """Per-scenario-template accuracy rollup.

    Keys are the ``PredictionRecord.scenario_template`` values
    (``"decision_forecast.yaml"``, ``"market_sim.yaml"``, etc.).
    Pairs whose prediction isn't in the lookup land under the
    sentinel key ``"<unknown>"`` — they're not silently dropped so the
    caller can see the size of the orphan tail.
    """

    def _by_template(record: PredictionRecord | None, _pair: CalibrationPair) -> str:
        return record.scenario_template if record else "<unknown>"

    return summarize_by(
        pairs,
        predictions_by_id=predictions_by_id,
        key_fn=_by_template,
        numeric_tolerance=numeric_tolerance,
    )


def per_anchor_namespace_summary(
    pairs: Iterable[CalibrationPair],
    *,
    predictions_by_id: dict[UUID, PredictionRecord],
    numeric_tolerance: float = 0.10,
) -> dict[str, CalibrationSummary]:
    """Per-anchor-namespace accuracy rollup.

    Anchor object ids look like ``"lease:LR-2026-117"`` /
    ``"deal:OPP-2026-44"``; the prefix before the first ``:`` is the
    object kind. Bucketing by namespace lets the Aggregate panel
    show "accuracy on leases vs. accuracy on deals" without callers
    threading domain metadata through the calibration loop.

    Anchors without a ``:`` collapse to the literal anchor string.
    """

    def _by_namespace(record: PredictionRecord | None, _pair: CalibrationPair) -> str:
        if record is None:
            return "<unknown>"
        anchor = record.anchor_object_id
        return anchor.split(":", 1)[0] if ":" in anchor else anchor

    return summarize_by(
        pairs,
        predictions_by_id=predictions_by_id,
        key_fn=_by_namespace,
        numeric_tolerance=numeric_tolerance,
    )


# ---------------------------------------------------------------------------
# Rolling time-window accuracy
# ---------------------------------------------------------------------------


def rolling_accuracy(
    pairs: Iterable[CalibrationPair],
    *,
    window: timedelta,
    now: datetime | None = None,
    predictions_by_id: dict[UUID, PredictionRecord] | None = None,
    numeric_tolerance: float = 0.10,
) -> CalibrationSummary:
    """Aggregate only pairs whose ``paired_at`` lands inside the trailing
    ``window`` from ``now`` (defaults to ``datetime.now(UTC)``).

    Returns a :class:`CalibrationSummary` covering the windowed slice.
    Empty windows return ``CalibrationSummary(n_pairs=0)`` — same shape
    :func:`aggregate_pairs` returns on an empty input — so the UI can
    render "no recent data" without a separate path.
    """
    if window <= timedelta(0):
        raise ValueError(f"window must be > 0, got {window}")
    end = now or datetime.now(UTC)
    start = end - window
    windowed = [p for p in pairs if start <= p.paired_at <= end]
    return aggregate_pairs(
        windowed,
        predictions_by_id=predictions_by_id,
        numeric_tolerance=numeric_tolerance,
    )


def rolling_accuracy_series(
    pairs: Iterable[CalibrationPair],
    *,
    window: timedelta,
    step: timedelta,
    now: datetime | None = None,
    predictions_by_id: dict[UUID, PredictionRecord] | None = None,
    numeric_tolerance: float = 0.10,
    horizon: timedelta | None = None,
) -> list[tuple[datetime, CalibrationSummary]]:
    """Return a list of ``(end_of_window, summary)`` pairs sliding back
    from ``now`` in ``step`` increments.

    ``horizon`` caps how far back the series extends (defaults to
    ``window`` — i.e. one bucket if unset). The list is ordered oldest
    first so the UI can render it as a left-to-right time series.
    """
    if step <= timedelta(0):
        raise ValueError(f"step must be > 0, got {step}")
    if window <= timedelta(0):
        raise ValueError(f"window must be > 0, got {window}")
    end = now or datetime.now(UTC)
    pairs_list = list(pairs)
    cutoff = end - (horizon or window)
    series: list[tuple[datetime, CalibrationSummary]] = []
    bucket_end = end
    while bucket_end >= cutoff:
        summary = rolling_accuracy(
            pairs_list,
            window=window,
            now=bucket_end,
            predictions_by_id=predictions_by_id,
            numeric_tolerance=numeric_tolerance,
        )
        series.append((bucket_end, summary))
        bucket_end -= step
    return list(reversed(series))


# ---------------------------------------------------------------------------
# Confidence drift across summaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceDrift:
    """Drift metrics across an ordered sequence of CalibrationSummary.

    ``delta`` is ``latest.confidence_calibration - earliest.confidence_calibration``.
    ``trend`` is ``"improving"`` when delta > 0, ``"degrading"`` when < 0,
    ``"flat"`` when |delta| < ``flat_threshold``. ``n_summaries`` counts
    how many points were considered (single-point input always returns
    ``"flat"`` with zero delta).
    """

    earliest: float
    latest: float
    delta: float
    trend: Literal["improving", "degrading", "flat"]
    n_summaries: int

    def as_wire_dict(self) -> dict[str, Any]:
        return {
            "earliest": round(self.earliest, 4),
            "latest": round(self.latest, 4),
            "delta": round(self.delta, 4),
            "trend": self.trend,
            "n_summaries": self.n_summaries,
        }


def confidence_drift(
    summaries: Sequence[CalibrationSummary],
    *,
    flat_threshold: float = 0.02,
) -> ConfidenceDrift:
    """Compare the first and last entries of ``summaries`` to spot drift.

    Callers pass the summaries in time order (oldest first); the
    function reports whether confidence calibration is climbing,
    degrading, or flat. Empty input returns a flat zero-delta record so
    the UI can show "no data yet" without a separate path.
    """
    if flat_threshold < 0:
        raise ValueError(f"flat_threshold must be >= 0, got {flat_threshold}")
    if not summaries:
        return ConfidenceDrift(
            earliest=0.0,
            latest=0.0,
            delta=0.0,
            trend="flat",
            n_summaries=0,
        )
    earliest = summaries[0].confidence_calibration
    latest = summaries[-1].confidence_calibration
    delta = latest - earliest
    trend: Literal["improving", "degrading", "flat"]
    if abs(delta) < flat_threshold:
        trend = "flat"
    elif delta > 0:
        trend = "improving"
    else:
        trend = "degrading"
    return ConfidenceDrift(
        earliest=earliest,
        latest=latest,
        delta=delta,
        trend=trend,
        n_summaries=len(summaries),
    )


# ---------------------------------------------------------------------------
# Modal outcome distribution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModalOutcomeDistribution:
    """Frequency table for modal-outcome values across a set of pairs.

    ``side`` selects which side of the pair to count
    (``"projected"`` or ``"actual"``). ``key`` is the outcome metric
    whose distribution is requested. ``counts`` maps the observed
    value (stringified) to its frequency. ``n`` is the total count.
    """

    side: Literal["projected", "actual"]
    key: str
    counts: dict[str, int] = field(default_factory=dict)
    n: int = 0

    def as_wire_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "key": self.key,
            "counts": dict(self.counts),
            "n": self.n,
        }


def modal_outcome_distribution(
    pairs: Iterable[CalibrationPair],
    *,
    key: str,
    side: Literal["projected", "actual"] = "actual",
) -> ModalOutcomeDistribution:
    """Count how many pairs land each distinct value of ``outcome[key]``.

    Use case: the Aggregate panel renders "of the 50 lease renewals
    Foresight predicted, 32 came in as ACCEPT, 14 RENEGOTIATE, 4
    REJECT". Pass ``side="actual"`` to count reality,
    ``side="projected"`` to count the model.
    """
    counts: dict[str, int] = {}
    n = 0
    for pair in pairs:
        outcome_map = pair.actual_outcome if side == "actual" else pair.projected_outcome
        if key not in outcome_map:
            continue
        value = str(outcome_map[key])
        counts[value] = counts.get(value, 0) + 1
        n += 1
    return ModalOutcomeDistribution(side=side, key=key, counts=counts, n=n)


# ---------------------------------------------------------------------------
# Convenience: build the per-id lookup that several primitives accept
# ---------------------------------------------------------------------------


def index_predictions(
    records: Iterable[PredictionRecord],
) -> dict[UUID, PredictionRecord]:
    """Return ``{record.id: record}`` — the lookup shape every grouping
    primitive accepts as ``predictions_by_id``.

    The cloud-side service builds this from the run's projection trail
    once per aggregation pass and reuses it across primitives. Keeping
    the helper here makes the construction symmetric to the rest of
    the aggregator surface.
    """
    return {record.id: record for record in records}


__all__ = [
    "ConfidenceDrift",
    "GroupKeyFn",
    "ModalOutcomeDistribution",
    "ThresholdDecision",
    "accuracy_meets_threshold",
    "confidence_drift",
    "group_pairs_by",
    "index_predictions",
    "modal_outcome_distribution",
    "per_anchor_namespace_summary",
    "per_scenario_template_summary",
    "rolling_accuracy",
    "rolling_accuracy_series",
    "summarize_by",
]
