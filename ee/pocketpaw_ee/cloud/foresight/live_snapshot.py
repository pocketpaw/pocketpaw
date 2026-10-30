# ee/pocketpaw_ee/cloud/foresight/live_snapshot.py
# Created: 2026-05-26 (feat/foresight-v10-live-snapshot-and-fixes) —
# RFC 08 v1.0 live-snapshot helpers. Pure functions only (no Beanie,
# no FastAPI, no engine imports) so the rules can be unit-tested
# without spinning up Mongo. The cloud service composes the
# :class:`LiveSnapshotView` from these helpers + the persisted run +
# projection rows, then maps it to the wire DTO.
#
# Three anomaly detector rules ship in v1.0:
#
#   1. ``tier_drift`` — actual tier mix vs the captain-locked
#      configured 5/15/80 default. Per-tier absolute deviation:
#        |actual - configured| > 0.15 → ``info``
#        |actual - configured| > 0.25 → ``warning``
#      One anomaly per drifting tier (so a run whose pool collapsed
#      to all-premium surfaces three rows — premium HIGH + mid + tail
#      LOW).
#
#   2. ``confidence_spike`` — confidence distribution skews extreme
#      across the run's projections. v1.0 rule:
#        variance < 0.02 AND mean > 0.8 → ``info``  (over-confident)
#        variance < 0.02 AND mean < 0.2 → ``info``  (under-confident)
#        mean < 0.2 AND sample_count >= 5 → ``warning`` (sustained low)
#
#   3. ``stalled_persona`` — a persona is silent / behind:
#        last decision ts > 30s behind run's latest tick ts → ``warning``
#        zero decisions while run reached >0 ticks → ``critical``
#
# The rules are independent — a single run can fire 0, 1, or many
# rows of different kinds. Callers cap the resulting list at the
# response level (the LivePanel spec has no hard cap; the detectors
# are bounded by the persona / tier count which is small in v1.0).
#
# Sampling helper: ``sample_traces`` picks up to N projections
# deterministically (sort by tick_id ASC + anchor_id ASC, then take
# evenly-spaced indices). Deterministic so re-fetches of an in-flight
# run produce a stable timeline slice across polls.

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, Literal

from pocketpaw_ee.cloud.foresight.domain import (
    LiveAnomaly,
    LiveSampledTrace,
    LiveTierMixActual,
)

# Captain-locked tier mix default (RFC 08 §10). Drift detection compares
# the actual per-tier share to this triple. Values mirror the
# ``DEFAULT_TIER_MIX`` the engine's :class:`TierMix.locked_default()`
# constructs — kept duplicated here so the cloud doesn't import the
# engine module just to read a literal.
DEFAULT_TIER_MIX: dict[str, float] = {
    "premium": 0.05,
    "mid": 0.15,
    "tail": 0.80,
}

# Anomaly thresholds — pinned in code so a test pinning the warning
# boundary stays stable across releases.
TIER_DRIFT_INFO_THRESHOLD: float = 0.15
TIER_DRIFT_WARNING_THRESHOLD: float = 0.25
CONFIDENCE_FLAT_VARIANCE: float = 0.02
CONFIDENCE_HIGH_MEAN: float = 0.80
CONFIDENCE_LOW_MEAN: float = 0.20
CONFIDENCE_LOW_WARN_SAMPLES: int = 5
STALL_GAP_SECONDS: float = 30.0
SAMPLED_TRACES_CAP: int = 10


# ---------------------------------------------------------------------------
# Trace sampling + sub-type-aware label rendering
# ---------------------------------------------------------------------------


def _sub_type_label(sub_type: str, decision_text: str, anchor_id: str) -> str:
    """Render ``action_summary`` for one projection.

    Sub-type-aware so the LivePanel matches the Tray's labelling
    convention. Shapes:

      - ``decision_forecast`` → just the decision text (the verb itself
        is the headline).
      - ``market_sim`` → ``"<segment> → <decision>"``.
      - ``org_change_rehearsal`` → ``"<rollout step>: <decision>"``.
      - Anything else → ``"<decision> @ <anchor>"`` as a neutral fallback.

    The 200-char cap is enforced at the DTO level; this helper keeps
    the body small enough that a normal anchor + verb tuple fits well
    under the limit, but a pathological anchor id won't blow the cap
    silently — the caller's :class:`SampledTrace` validation will 422
    if it ever happens.
    """
    decision_text = (decision_text or "").strip()
    if sub_type == "decision_forecast":
        return decision_text or "(no decision)"
    if sub_type == "market_sim":
        segment = _anchor_suffix(anchor_id, "segment") or anchor_id
        return f"{segment} -> {decision_text}".strip()
    if sub_type == "org_change_rehearsal":
        rollout = _anchor_suffix(anchor_id, "rollout") or anchor_id
        return f"{rollout}: {decision_text}".strip()
    return f"{decision_text} @ {anchor_id}".strip()


def _anchor_suffix(anchor_id: str, expected_prefix: str) -> str:
    """Strip the ``<prefix>:`` namespace off an anchor id."""
    if ":" not in anchor_id:
        return anchor_id
    prefix, _, suffix = anchor_id.partition(":")
    if prefix == expected_prefix:
        return suffix
    return anchor_id


def sample_traces(
    projections: list[Any],
    *,
    cap: int = SAMPLED_TRACES_CAP,
) -> list[LiveSampledTrace]:
    """Pick up to ``cap`` projections deterministically.

    Order: ``(tick_id ASC, anchor_id ASC)`` — same as the persistence
    index, so the slice is a stable window over the run's timeline.
    When the run has more than ``cap`` projections, we take evenly-spaced
    indices so the slice spans the whole timeline rather than just the
    first ``cap`` rows (an in-flight run with 500 projections should
    still surface the most recent tick on the LivePanel).

    The input is the list of projection-domain objects (or any
    duck-typed equivalent exposing ``tick_id`` / ``anchor_id`` /
    ``persona_id`` / ``decision_text`` / ``confidence`` / ``sub_type``).
    The helper stays domain-typed so the service call site is one line.
    """
    if not projections or cap <= 0:
        return []

    sorted_proj = sorted(
        projections,
        key=lambda p: (
            int(getattr(p, "tick_id", 0) or 0),
            str(getattr(p, "anchor_id", "") or ""),
        ),
    )
    n = len(sorted_proj)
    if n <= cap:
        picks = sorted_proj
    else:
        # Evenly-spaced indices: 0, n/cap, 2n/cap, ..., (cap-1)*n/cap.
        # Floor-division keeps the indices integer; the last pick is
        # always the latest tick so the LivePanel always shows "what's
        # happening now".
        step = n / cap
        picks = [sorted_proj[min(int(i * step), n - 1)] for i in range(cap)]
        # Force the last pick to be the latest projection (the timeline's
        # head) so an in-flight panel surfaces fresh content.
        picks[-1] = sorted_proj[-1]

    out: list[LiveSampledTrace] = []
    for proj in picks:
        sub_type = str(getattr(proj, "sub_type", "") or "decision_forecast")
        decision_text = str(getattr(proj, "decision_text", "") or "")
        anchor_id = str(getattr(proj, "anchor_id", "") or "")
        summary = _sub_type_label(sub_type, decision_text, anchor_id)
        # Hard-clip at 200 chars so the DTO never 422s on a
        # pathological anchor / verb. The DTO's max_length is the
        # contract; the clip here is defensive.
        summary = summary[:200]
        out.append(
            LiveSampledTrace(
                tick_id=int(getattr(proj, "tick_id", 0) or 0),
                persona_id=str(getattr(proj, "persona_id", "") or ""),
                sub_type=sub_type,
                action_summary=summary,
                confidence=float(getattr(proj, "confidence", 0.0) or 0.0),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Tier mix derivation
# ---------------------------------------------------------------------------


def derive_tier_mix_actual(
    *,
    projections: list[Any] | None = None,
    run_result: dict[str, Any] | None = None,
) -> LiveTierMixActual:
    """Derive the actual tier mix triple from the run state.

    Source priority:
      1. ``run_result['tier_distribution']`` — the engine reports
         per-tier persona count under this key when a tier pool was
         used. This is the canonical source when present.
      2. Tier inference from projection ``persona_id`` distribution —
         the v0.5 engine doesn't yet tag projections with the tier the
         persona ran on, so this branch returns zeros across all
         tiers. v1.1+ will surface a per-projection tier label and the
         derivation can fall back to a count over distinct
         ``(persona_id, tier)`` pairs.
      3. Zero fallback — empty run, never raises.

    Returns a :class:`LiveTierMixActual` with the three shares.
    """
    counts: dict[str, int] = {}
    total = 0

    if isinstance(run_result, dict):
        block = run_result.get("tier_distribution")
        if isinstance(block, dict):
            for tier, count in block.items():
                if not isinstance(count, int):
                    continue
                counts[str(tier)] = counts.get(str(tier), 0) + int(count)
                total += int(count)

    # v0.5 engine never tags projections with tiers; the fallback is a
    # zero triple. Kept as a placeholder so v1.1 doesn't have to
    # rewire the derivation site.
    if total == 0 and projections:
        # No-op placeholder — when projections carry a tier field in
        # v1.1, the derivation will read it here. For now stay at
        # ``total == 0`` so we land in the zero branch.
        pass

    if total == 0:
        return LiveTierMixActual(premium=0.0, mid=0.0, tail=0.0)

    return LiveTierMixActual(
        premium=round(counts.get("premium", 0) / total, 4),
        mid=round(counts.get("mid", 0) / total, 4),
        tail=round(counts.get("tail", 0) / total, 4),
    )


# ---------------------------------------------------------------------------
# Anomaly detectors — each is a pure function returning ``list[LiveAnomaly]``.
# ---------------------------------------------------------------------------


def detect_tier_drift(
    actual: LiveTierMixActual,
    *,
    configured: dict[str, float] | None = None,
) -> list[LiveAnomaly]:
    """Flag per-tier drift vs the configured mix.

    Empty / zero actual mix surfaces no rows — the caller shouldn't
    spam the panel with "you have 0% premium and 5% expected" on a
    fresh run. Only fires when the actual sum is non-trivial
    (``sum(actual.*) >= 0.5`` — half the population assigned, i.e.
    the run has progressed enough that the mix is meaningful).
    """
    configured_mix = configured or DEFAULT_TIER_MIX
    total_share = actual.premium + actual.mid + actual.tail
    if total_share < 0.5:
        return []

    out: list[LiveAnomaly] = []
    for tier in ("premium", "mid", "tail"):
        expected = float(configured_mix.get(tier, 0.0))
        actual_value = float(getattr(actual, tier, 0.0))
        deviation = abs(actual_value - expected)
        severity: Literal["info", "warning", "critical"]
        if deviation > TIER_DRIFT_WARNING_THRESHOLD:
            severity = "warning"
        elif deviation > TIER_DRIFT_INFO_THRESHOLD:
            severity = "info"
        else:
            continue
        direction = "above" if actual_value > expected else "below"
        body = (
            f"Tier '{tier}' is {direction} the configured {expected:.2f} "
            f"target (actual {actual_value:.2f}, deviation {deviation:.2f})."
        )
        # The 240-char DTO cap is generous; trim defensively anyway.
        out.append(LiveAnomaly(kind="tier_drift", severity=severity, body=body[:240]))
    return out


def detect_confidence_spike(projections: list[Any]) -> list[LiveAnomaly]:
    """Flag confidence-distribution anomalies.

    Computes mean + (population) variance over the projections'
    ``confidence`` floats. Empty / single-row runs surface no rows
    (variance is undefined or trivially zero).
    """
    confidences: list[float] = []
    for proj in projections:
        value = getattr(proj, "confidence", None)
        if not isinstance(value, (int, float)):
            continue
        confidences.append(float(value))
    n = len(confidences)
    if n < 2:
        return []

    mean = sum(confidences) / n
    variance = sum((c - mean) ** 2 for c in confidences) / n

    out: list[LiveAnomaly] = []
    if variance < CONFIDENCE_FLAT_VARIANCE and mean > CONFIDENCE_HIGH_MEAN:
        out.append(
            LiveAnomaly(
                kind="confidence_spike",
                severity="info",
                body=(
                    f"Confidence flat-high (mean {mean:.2f}, "
                    f"variance {variance:.4f}). Personas may be "
                    "over-aligned or the LLM is rate-limiting variance."
                ),
            )
        )
    if variance < CONFIDENCE_FLAT_VARIANCE and mean < CONFIDENCE_LOW_MEAN:
        out.append(
            LiveAnomaly(
                kind="confidence_spike",
                severity="info",
                body=(
                    f"Confidence flat-low (mean {mean:.2f}, "
                    f"variance {variance:.4f}). The cohort isn't taking "
                    "a clear position; revisit persona drafting."
                ),
            )
        )
    if mean < CONFIDENCE_LOW_MEAN and n >= CONFIDENCE_LOW_WARN_SAMPLES:
        out.append(
            LiveAnomaly(
                kind="confidence_spike",
                severity="warning",
                body=(
                    f"Sustained low confidence (mean {mean:.2f} across "
                    f"{n} samples). The scenario isn't producing actionable "
                    "projections — review anchors + prompts."
                ),
            )
        )
    return out


def detect_stalled_persona(
    projections: list[Any],
    *,
    latest_tick_id: int | None = None,
    latest_tick_ts: datetime | None = None,
) -> list[LiveAnomaly]:
    """Flag personas that fell behind the run's tick clock.

    Two rules:

      1. ``stalled_persona`` (warning) — a persona has at least one
         projection, but their most recent ``createdAt`` is more than
         :data:`STALL_GAP_SECONDS` behind ``latest_tick_ts``.
      2. ``stalled_persona`` (critical) — a persona has ZERO
         projections while the run has reached ``latest_tick_id > 0``.

    ``latest_tick_id == 0`` (or ``None``) collapses to no rows —
    the run hasn't ticked yet, nothing's stalled.

    The function operates on duck-typed projection objects exposing
    ``persona_id`` and ``created_at`` (or ``createdAt``). Missing /
    None timestamps are tolerated — those personas are simply not
    considered for the warning rule.
    """
    if latest_tick_id is None or latest_tick_id <= 0:
        return []

    last_seen_by_persona: dict[str, datetime | None] = {}
    for proj in projections:
        persona_id = str(getattr(proj, "persona_id", "") or "")
        if not persona_id:
            continue
        ts = getattr(proj, "created_at", None) or getattr(proj, "createdAt", None)
        if not isinstance(ts, datetime):
            ts = None
        prev = last_seen_by_persona.get(persona_id)
        if prev is None or (ts is not None and (prev is None or ts > prev)):
            last_seen_by_persona[persona_id] = ts

    out: list[LiveAnomaly] = []
    if isinstance(latest_tick_ts, datetime):
        gap = timedelta(seconds=STALL_GAP_SECONDS)
        for persona_id, last_ts in last_seen_by_persona.items():
            if not isinstance(last_ts, datetime):
                continue
            if (latest_tick_ts - last_ts) > gap:
                out.append(
                    LiveAnomaly(
                        kind="stalled_persona",
                        severity="warning",
                        body=(
                            f"Persona '{persona_id}' last decision was "
                            f"{(latest_tick_ts - last_ts).total_seconds():.0f}s "
                            f"behind the run's latest tick — investigate "
                            "rate-limit / queue depth."
                        )[:240],
                    )
                )
    # ``critical`` rule — silent personas. The detector doesn't know
    # the full persona roster without the run's request body; the
    # caller threads in the expected persona ids via the wrapper
    # below.
    return out


def detect_silent_personas(
    *,
    expected_persona_ids: Iterable[str],
    seen_persona_ids: Iterable[str],
    latest_tick_id: int | None,
) -> list[LiveAnomaly]:
    """Flag personas that have zero projections while the run reached
    tick > 0 (``critical`` severity).

    Kept separate from :func:`detect_stalled_persona` so the silent-
    persona rule can be invoked even when the caller doesn't have
    every projection in memory (e.g. for a very large run, the cloud
    service walks the persona id set against
    ``COUNT(*) GROUP BY persona_id`` rather than loading every
    projection — v1.1 path).
    """
    if latest_tick_id is None or latest_tick_id <= 0:
        return []
    expected = {pid for pid in expected_persona_ids if pid}
    seen = {pid for pid in seen_persona_ids if pid}
    silent = expected - seen
    return [
        LiveAnomaly(
            kind="stalled_persona",
            severity="critical",
            body=(
                f"Persona '{persona_id}' produced zero projections while "
                f"the run reached tick {latest_tick_id}. The cohort is "
                "missing a participant."
            )[:240],
        )
        for persona_id in sorted(silent)
    ]


def detect_all_anomalies(
    *,
    tier_mix_actual: LiveTierMixActual,
    projections: list[Any],
    expected_persona_ids: Iterable[str],
    latest_tick_id: int | None,
    latest_tick_ts: datetime | None,
    configured_mix: dict[str, float] | None = None,
) -> list[LiveAnomaly]:
    """Run every v1.0 detector rule and concatenate the results.

    Order: tier_drift → confidence_spike → stalled_persona (warning)
    → stalled_persona (critical). The LivePanel sorts client-side by
    severity, so the in-list order is just a stable concatenation
    convention.
    """
    seen_personas = {
        str(getattr(p, "persona_id", "") or "") for p in projections if getattr(p, "persona_id", "")
    }
    out: list[LiveAnomaly] = []
    out.extend(detect_tier_drift(tier_mix_actual, configured=configured_mix))
    out.extend(detect_confidence_spike(projections))
    out.extend(
        detect_stalled_persona(
            projections,
            latest_tick_id=latest_tick_id,
            latest_tick_ts=latest_tick_ts,
        )
    )
    out.extend(
        detect_silent_personas(
            expected_persona_ids=expected_persona_ids,
            seen_persona_ids=seen_personas,
            latest_tick_id=latest_tick_id,
        )
    )
    return out


__all__ = [
    "CONFIDENCE_FLAT_VARIANCE",
    "CONFIDENCE_HIGH_MEAN",
    "CONFIDENCE_LOW_MEAN",
    "CONFIDENCE_LOW_WARN_SAMPLES",
    "DEFAULT_TIER_MIX",
    "SAMPLED_TRACES_CAP",
    "STALL_GAP_SECONDS",
    "TIER_DRIFT_INFO_THRESHOLD",
    "TIER_DRIFT_WARNING_THRESHOLD",
    "derive_tier_mix_actual",
    "detect_all_anomalies",
    "detect_confidence_spike",
    "detect_silent_personas",
    "detect_stalled_persona",
    "detect_tier_drift",
    "sample_traces",
]
