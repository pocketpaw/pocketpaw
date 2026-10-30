# ee/pocketpaw_ee/foresight/insights.py
# Created: 2026-05-25 (feat/foresight-v15-scenarios-aggregate-insights) —
# RFC 08 §11.6 Insights panel backing module.
#
# Pattern-based insight synthesizer — pure functions over aggregator
# inputs. v0.1 ships five rules:
#
#   - ``accuracy_drop``        — rolling accuracy week-over-week delta
#                                < -0.10 → warning.
#   - ``persona_outlier``      — any per-persona calibration < 0.50 →
#                                warning.
#   - ``tier_imbalance``       — actual vs configured tier mix delta
#                                > 0.15 on any tier → info.
#   - ``trend_break``          — confidence-drift |magnitude| > 0.20 →
#                                warning.
#   - ``threshold_unmet``      — most recent backtest's
#                                ``gate_decision.passed == false`` →
#                                critical.
#
# Stable ids are formatted ``{kind}_{period_key}`` so re-runs against
# the same aggregate snapshot don't duplicate. The list is sorted by
# severity descending (critical > warning > info) then ``generated_at``
# descending; callers cap at 20 items per response.
#
# Pure / no I/O / no async — same shape as ``aggregator.py``. The cloud
# service composes the aggregate inputs once (rolling series,
# confidence drift, modal distribution, latest backtest gate, per-tier
# mix, per-persona calibration) and passes them in; this module owns
# only the rule logic so the synthesizer is easy to unit-test in
# isolation.

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

InsightKind = Literal[
    "accuracy_drop",
    "persona_outlier",
    "tier_imbalance",
    "trend_break",
    "threshold_unmet",
]

InsightSeverity = Literal["info", "warning", "critical"]

# Severity rank for the sort comparator — critical first, then warning,
# then info. Used as ``-_SEVERITY_RANK[insight.severity]`` so the
# resulting sort key produces descending severity with a single ``sort``
# call.
_SEVERITY_RANK: dict[str, int] = {"critical": 3, "warning": 2, "info": 1}


@dataclass(frozen=True)
class Insight:
    """One synthesized insight record.

    ``id`` is stable across runs of the synthesizer against the same
    aggregate snapshot — same ``{kind}_{period_key}`` shape means the
    Insights panel can dedupe across polls. ``anchor_refs`` carries
    the optional anchor / pocket references the UI links to (e.g.
    ``anchor:rollout:training`` for an org-change rollout insight).
    """

    id: str
    kind: InsightKind
    title: str
    body: str
    severity: InsightSeverity
    generated_at: datetime
    anchor_refs: tuple[str, ...] = ()

    def as_wire_dict(self) -> dict[str, Any]:
        """Wire-shape dict; ISO-8601 string for ``generated_at``."""
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "anchor_refs": list(self.anchor_refs),
            "generated_at": _iso(self.generated_at),
        }


# ---------------------------------------------------------------------------
# Rule inputs — kept as plain dataclasses so the synthesizer signature
# is explicit. The cloud service builds these from its aggregator
# composition and hands them in; tests construct them directly.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RollingAccuracyPoint:
    """One time-bucketed accuracy reading."""

    ts: datetime
    accuracy: float
    sample_count: int


@dataclass(frozen=True)
class ConfidenceDriftInput:
    """The drift summary the synthesizer reads.

    ``trend`` is the bucket label ("rising" / "falling" / "flat") the
    aggregator emits; ``magnitude`` is the absolute drift size.
    """

    trend: Literal["rising", "falling", "flat"]
    magnitude: float


@dataclass(frozen=True)
class PerPersonaCalibration:
    """One persona's calibration score over the window."""

    persona_id: str
    calibration: float
    sample_count: int = 0


@dataclass(frozen=True)
class TierDistributionDelta:
    """Per-tier configured vs actual mix delta.

    ``configured`` + ``actual`` are fractions in [0.0, 1.0]; ``delta``
    is ``actual - configured`` (signed). The rule fires on |delta| >
    threshold; the sign is preserved so the body copy can describe
    direction ("tier_imbalance: actual premium share 0.40 vs configured
    0.05 — +0.35 over").
    """

    tier: str
    configured: float
    actual: float

    @property
    def delta(self) -> float:
        return self.actual - self.configured


@dataclass(frozen=True)
class LatestBacktestGate:
    """Minimal view of the workspace's most recent backtest.

    Threshold-unmet only needs ``passed`` + ``observed`` + ``threshold``
    + the backtest id to render the insight; the full doc isn't required.
    """

    backtest_id: str
    passed: bool
    observed: float
    threshold: float
    completed_at: datetime | None = None


@dataclass(frozen=True)
class SynthesizerInput:
    """Bundle of inputs the synthesizer consumes.

    Every field is optional so partial data (empty workspace, no
    backtests yet, only one accuracy bucket) doesn't break the synth —
    rules that can't compute simply yield no insights.

    ``now`` anchors every ``generated_at`` so tests stay deterministic;
    callers default to ``datetime.now(UTC)``.
    """

    now: datetime
    rolling_accuracy: Sequence[RollingAccuracyPoint] = ()
    confidence_drift: ConfidenceDriftInput | None = None
    per_persona_calibration: Sequence[PerPersonaCalibration] = ()
    tier_distribution_deltas: Sequence[TierDistributionDelta] = ()
    latest_backtest: LatestBacktestGate | None = None
    period_key: str = ""

    # Per-rule overrides (kept on the input bundle so callers don't have
    # to pass a separate config). Defaults match the RFC spec.
    accuracy_drop_threshold: float = -0.10
    persona_outlier_threshold: float = 0.50
    tier_imbalance_threshold: float = 0.15
    trend_break_threshold: float = 0.20


# ---------------------------------------------------------------------------
# Synthesizer entry point
# ---------------------------------------------------------------------------


def synthesize_insights(
    bundle: SynthesizerInput,
    *,
    cap: int = 20,
) -> list[Insight]:
    """Run the five v0.1 rules against ``bundle`` and return a sorted
    list of insights.

    Order: by severity descending (critical > warning > info), then by
    ``generated_at`` descending. Callers should respect ``cap`` — the
    RFC §11.6 v0.1 contract caps the response at 20 items. A larger
    cap is allowed for unit tests that want to inspect every emitted
    rule without losing tail records.
    """
    if cap < 1:
        raise ValueError(f"cap must be >= 1, got {cap}")

    period_key = bundle.period_key or _default_period_key(bundle.now)

    emitted: list[Insight] = []
    emitted.extend(_rule_accuracy_drop(bundle, period_key))
    emitted.extend(_rule_persona_outlier(bundle, period_key))
    emitted.extend(_rule_tier_imbalance(bundle, period_key))
    emitted.extend(_rule_trend_break(bundle, period_key))
    emitted.extend(_rule_threshold_unmet(bundle, period_key))

    # Sort: severity desc, then generated_at desc. Stable sort + tuple
    # comparator keeps siblings in deterministic order across runs of
    # the same input bundle.
    emitted.sort(
        key=lambda item: (
            -_SEVERITY_RANK.get(item.severity, 0),
            -item.generated_at.timestamp(),
            item.id,
        )
    )
    return emitted[:cap]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _rule_accuracy_drop(
    bundle: SynthesizerInput,
    period_key: str,
) -> list[Insight]:
    """RFC §11.6 rule 1 — week-over-week accuracy drop > 10%.

    Compares the most recent ``RollingAccuracyPoint`` against the
    earliest in the window. A delta below the configured threshold
    (default -0.10) fires a ``warning`` insight. Fewer than two points
    means no comparison is possible — yields nothing.
    """
    points = list(bundle.rolling_accuracy)
    if len(points) < 2:
        return []
    # Sort by ts so earliest/latest are unambiguous even if callers
    # passed an unordered list.
    points.sort(key=lambda p: p.ts)
    earliest = points[0]
    latest = points[-1]
    delta = latest.accuracy - earliest.accuracy
    if delta >= bundle.accuracy_drop_threshold:
        return []
    title = (
        f"Accuracy fell {abs(delta) * 100:.0f}% week over week"
        if abs(delta) >= 0.10
        else f"Accuracy dipped by {abs(delta):.2f}"
    )
    body = (
        f"Modal accuracy dropped from {earliest.accuracy:.2f} "
        f"({earliest.sample_count} samples) at {_iso(earliest.ts)} to "
        f"{latest.accuracy:.2f} ({latest.sample_count} samples) at "
        f"{_iso(latest.ts)}."
    )
    return [
        Insight(
            id=f"accuracy_drop_{period_key}",
            kind="accuracy_drop",
            title=title,
            body=body,
            severity="warning",
            generated_at=bundle.now,
            anchor_refs=(),
        )
    ]


def _rule_persona_outlier(
    bundle: SynthesizerInput,
    period_key: str,
) -> list[Insight]:
    """RFC §11.6 rule 2 — per-persona calibration below the floor.

    Fires one ``warning`` insight per persona whose calibration falls
    below the configured threshold (default 0.50). Persona ids are
    surfaced in ``anchor_refs`` so the UI can link the Insights row to
    the Live panel's persona drawer.
    """
    insights: list[Insight] = []
    for entry in bundle.per_persona_calibration:
        if entry.calibration >= bundle.persona_outlier_threshold:
            continue
        # Per-persona id collision-safe — the period key plus the persona
        # id gives a unique id per (period, persona) bucket.
        safe_id = _safe_id_segment(entry.persona_id) or "unknown"
        body = (
            f"Persona {entry.persona_id} has calibration {entry.calibration:.2f} "
            f"({entry.sample_count} samples), below the {bundle.persona_outlier_threshold:.2f} "
            "floor. Review the persona seed or expand the calibration buffer."
        )
        insights.append(
            Insight(
                id=f"persona_outlier_{period_key}_{safe_id}",
                kind="persona_outlier",
                title=f"Persona outlier: {entry.persona_id}",
                body=body,
                severity="warning",
                generated_at=bundle.now,
                anchor_refs=(f"persona:{entry.persona_id}",),
            )
        )
    return insights


def _rule_tier_imbalance(
    bundle: SynthesizerInput,
    period_key: str,
) -> list[Insight]:
    """RFC §11.6 rule 3 — actual vs configured tier mix delta.

    Fires one ``info`` insight per tier whose absolute delta exceeds
    the configured threshold (default 0.15). The body copy describes
    direction so the operator sees whether actual usage skewed
    high-tier (cost overrun) or low-tier (under-utilization of the
    premium pool).
    """
    insights: list[Insight] = []
    for entry in bundle.tier_distribution_deltas:
        if abs(entry.delta) <= bundle.tier_imbalance_threshold:
            continue
        direction = "above" if entry.delta > 0 else "below"
        safe_id = _safe_id_segment(entry.tier) or "tier"
        body = (
            f"Tier '{entry.tier}' actual share {entry.actual:.2f} is "
            f"{abs(entry.delta):.2f} {direction} configured {entry.configured:.2f}. "
            "Review tier_mix overrides or recalibrate the pool."
        )
        insights.append(
            Insight(
                id=f"tier_imbalance_{period_key}_{safe_id}",
                kind="tier_imbalance",
                title=f"Tier imbalance: {entry.tier}",
                body=body,
                severity="info",
                generated_at=bundle.now,
                anchor_refs=(f"tier:{entry.tier}",),
            )
        )
    return insights


def _rule_trend_break(
    bundle: SynthesizerInput,
    period_key: str,
) -> list[Insight]:
    """RFC §11.6 rule 4 — confidence-drift magnitude exceeds threshold.

    The brief defines the trigger as ``|magnitude| > 0.20``. The
    ``trend`` label is included in the body so the UI knows whether
    the drift is rising or falling, even though the rule fires on the
    absolute size.
    """
    drift = bundle.confidence_drift
    if drift is None:
        return []
    if abs(drift.magnitude) <= bundle.trend_break_threshold:
        return []
    body = (
        f"Confidence drift {drift.trend} with magnitude {drift.magnitude:.2f}, "
        f"above the {bundle.trend_break_threshold:.2f} break threshold. "
        "Inspect the per-scenario rollups for the underlying shift."
    )
    return [
        Insight(
            id=f"trend_break_{period_key}",
            kind="trend_break",
            title=f"Confidence trend broke ({drift.trend})",
            body=body,
            severity="warning",
            generated_at=bundle.now,
            anchor_refs=(),
        )
    ]


def _rule_threshold_unmet(
    bundle: SynthesizerInput,
    period_key: str,
) -> list[Insight]:
    """RFC §11.6 rule 5 — most recent backtest's gate failed.

    Fires a ``critical`` insight when the latest completed backtest's
    ``gate_decision.passed`` is ``False``. The anchor ref points at
    the backtest id so the UI can deep-link to the Aggregate panel's
    backtest row.
    """
    gate = bundle.latest_backtest
    if gate is None or gate.passed:
        return []
    completed_iso = _iso(gate.completed_at) if gate.completed_at else "unknown"
    body = (
        f"Backtest {gate.backtest_id} scored {gate.observed:.2f} against the "
        f"{gate.threshold:.2f} gate (completed {completed_iso}). Forward sims "
        "are blocked until a passing backtest unlocks the gate."
    )
    return [
        Insight(
            id=f"threshold_unmet_{period_key}",
            kind="threshold_unmet",
            title="Onboarding gate unmet",
            body=body,
            severity="critical",
            generated_at=bundle.now,
            anchor_refs=(f"backtest:{gate.backtest_id}",),
        )
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_period_key(now: datetime) -> str:
    """Compute the stable ISO year-week key for ``now``.

    Format: ``YYYY_WNN`` (e.g. ``2026_W21``) so the persisted id format
    in the RFC ("accuracy_drop_2026_W21") falls out automatically when
    callers don't pre-supply a ``period_key``. Using ``isocalendar`` keeps
    the boundary stable across the year roll-over.
    """
    iso = now.isocalendar()
    return f"{iso.year}_W{iso.week:02d}"


def _safe_id_segment(value: str) -> str:
    """Sanitize a string for inclusion in an Insight id.

    Replaces every character outside ``[A-Za-z0-9_-]`` with ``_`` so the
    composed id stays URL-safe + diff-stable across runs.
    """
    out_chars: list[str] = []
    for ch in value:
        if ch.isalnum() or ch in ("_", "-"):
            out_chars.append(ch)
        else:
            out_chars.append("_")
    return "".join(out_chars).strip("_")


def _iso(dt: datetime | None) -> str:
    """ISO-8601 formatter that always returns a string (empty when
    ``None``). Kept local to avoid pulling the cloud-side ``iso_utc``
    helper into the engine layer (this module must stay clean of
    pocketpaw_ee.cloud per the import-linter contract).
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat().replace("+00:00", "Z")


def index_insights_by_kind(insights: Iterable[Insight]) -> dict[str, list[Insight]]:
    """Bucket insights by kind — handy when the UI wants to render
    sections per kind without re-walking the flat list.

    Insertion order is preserved within each bucket so the synthesizer's
    severity-descending order survives the regrouping.
    """
    out: dict[str, list[Insight]] = {}
    for ins in insights:
        out.setdefault(ins.kind, []).append(ins)
    return out


__all__ = [
    "ConfidenceDriftInput",
    "Insight",
    "InsightKind",
    "InsightSeverity",
    "LatestBacktestGate",
    "PerPersonaCalibration",
    "RollingAccuracyPoint",
    "SynthesizerInput",
    "TierDistributionDelta",
    "index_insights_by_kind",
    "synthesize_insights",
]
