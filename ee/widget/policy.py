# ee/widget/policy.py — Graduation + co-occurrence thresholds over the
# widget projection.
# Created: 2026-04-16 (feat/widget-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Ports #941's pin / fade / archive decision
# logic from a JSONL scan onto a projection scan, and #942's
# co-occurrence-detector-as-threshold onto the same projection. Every
# tuning knob that was public in the held PRs is preserved here under
# the same name so existing config knobs and tests carry over without
# rename.
#
# Why keep the thresholds verbatim? Because #941 + #942 shipped tuning
# defaults that were picked for feel. The refactor isn't the place to
# re-tune — a follow-up can make them per-pocket configurable.
#
# Two decision flows, both over the same WidgetProjection:
#
#   - scan_for_widget_graduations — pin / fade / archive per (widget,
#     surface) pair. Ports #941.
#   - scan_for_cooccurrences — signature pairs above threshold.
#     Ports #942 with the sorted(tokens)[:6] fix (applied in the
#     projection, not recomputed here — the policy trusts the
#     projection's signatures).
#
# Note on soul-protocol's AgentProposal/HumanCorrection primitives
# (spec/decisions.py): a widget graduation is a *system-emitted*
# decision derived from usage counts, not a human-reviewed proposal,
# so the AgentProposal shape is not a great fit (no summary, no
# reviewer disposition). We stay on ``widget.graduated`` events. If a
# future slice introduces human-in-the-loop approval for widget
# promotion (captain reviews proposed pins before they apply), it can
# wrap these decisions in agent.proposed / human.corrected pairs
# without disturbing the projection.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ee.widget.projection import WidgetProjection
from ee.widget.store import WidgetJournalStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning defaults — carried verbatim from #941 + #942 so the runtime
# feel stays identical post-refactor.
# ---------------------------------------------------------------------------

# #941 graduation knobs
DEFAULT_WINDOW_DAYS = 30
DEFAULT_PIN_THRESHOLD = 10          # Promoting interactions in window → pin
DEFAULT_ARCHIVE_DAYS = 60           # Untouched longer than this → archive
_PROMOTING_ACTIONS = ("open", "edit", "click")

# #942 co-occurrence knobs
DEFAULT_COOCCURRENCE_WINDOW_DAYS = 7
DEFAULT_COOCCURRENCE_THRESHOLD = 3
DEFAULT_SESSION_GAP_SECONDS = 15 * 60  # 15-minute session window (#942)


WidgetTier = Literal["pin", "fade", "archive"]


@dataclass
class WidgetGraduationDecision:
    """One proposed widget tier change.

    Mirrors #941's WidgetDecision shape so downstream consumers
    (paw-enterprise SuggestedWidgetsFeed UI in issue #74) don't need
    to learn a new dataclass. ``tier`` carries the verdict.
    """

    widget_name: str
    surface: str
    tier: WidgetTier
    confidence: float
    interactions_in_window: int
    window_days: int
    previous_tier: str | None = None
    pocket_id: str | None = None
    scope: list[str] = field(default_factory=list)
    reason: str = ""

    def short(self) -> str:
        """One-liner for terminal output / operator dashboards."""

        prev = self.previous_tier or "?"
        return (
            f"[{self.tier}] {self.widget_name}@{self.surface} {prev}->{self.tier} "
            f"({self.interactions_in_window} hits in {self.window_days}d, "
            f"conf={self.confidence:.2f})"
        )


@dataclass
class WidgetGraduationReport:
    """Output of one graduation scan — decisions + scan metadata."""

    decisions: list[WidgetGraduationDecision] = field(default_factory=list)
    scanned_widgets: int = 0
    window_days: int = DEFAULT_WINDOW_DAYS
    dry_run: bool = True
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class CooccurrenceCandidate:
    """One co-occurring-pair candidate surfaced by the threshold scan.

    Ports the PatternMatch + SuggestedWidget split from #942 into a
    single dataclass — the policy emits candidates; callers that want
    the richer "proposed widget" shape (title, description,
    confidence) can map them downstream without the policy owning UI
    copy.
    """

    signature: str
    widget_a: str
    widget_b: str
    count: int
    window_s: int
    pocket_id: str | None = None
    scope: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class CooccurrenceReport:
    candidates: list[CooccurrenceCandidate] = field(default_factory=list)
    scanned_pairs: int = 0
    threshold: int = DEFAULT_COOCCURRENCE_THRESHOLD
    window_s: int = DEFAULT_SESSION_GAP_SECONDS
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Scan — graduation. Counts per-widget promoting interactions off the
# projection and emits decisions that cross the configured thresholds.
# ---------------------------------------------------------------------------


def scan_for_widget_graduations(
    projection: WidgetProjection,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    pin_threshold: int = DEFAULT_PIN_THRESHOLD,
    archive_days: int = DEFAULT_ARCHIVE_DAYS,
    pocket_id: str | None = None,
    scope: str | None = None,
    promoting_actions: tuple[str, ...] = _PROMOTING_ACTIONS,
    dry_run: bool = True,
) -> WidgetGraduationReport:
    """Walk the projection's usage roll-up and emit pin / fade /
    archive decisions.

    Pin: widget hit ``pin_threshold`` promoting interactions in the
        window.
    Fade: widget seen at least once historically but zero promoting
        interactions in the window.
    Archive: widget last touched more than ``archive_days`` ago.

    Thresholds carried verbatim from #941 — when an operator wants a
    different cadence, pass kwargs. This path never writes; apply()
    does. Same dry-run-by-default contract as #941 so the captain
    reviews the report before committing tier changes.
    """

    now = datetime.now(UTC)
    window_cutoff = now - timedelta(days=window_days)
    archive_cutoff = now - timedelta(days=archive_days)

    usage = projection.usage(
        window_days=max(window_days, archive_days),
        scope=scope,
        pocket_id=pocket_id,
        promoting_actions=promoting_actions,
    )

    decisions: list[WidgetGraduationDecision] = []
    for row in usage:
        last = _ensure_aware(row.last_interaction)

        # Pull the existing tier (if any) so the decision carries prior
        # state. The projection keeps only the latest verdict per
        # widget; querying it here is cheap.
        prior = projection.graduation_state(
            widget_name=row.widget_name,
            surface=row.surface,
        )
        previous_tier = prior[0].current_tier if prior else None

        if row.promoting_count >= pin_threshold and last >= window_cutoff:
            if previous_tier == "pin":
                # Already pinned — skip so we don't re-emit the same
                # decision on every scan.
                continue
            confidence = min(1.0, row.promoting_count / (pin_threshold * 3))
            decisions.append(
                WidgetGraduationDecision(
                    widget_name=row.widget_name,
                    surface=row.surface,
                    tier="pin",
                    confidence=confidence,
                    interactions_in_window=row.promoting_count,
                    window_days=window_days,
                    previous_tier=previous_tier,
                    pocket_id=row.pocket_id,
                    scope=list(row.scope),
                    reason=(
                        f"Opened/edited/clicked {row.promoting_count}x in last "
                        f"{window_days} days (threshold {pin_threshold})."
                    ),
                )
            )
            continue

        if last < archive_cutoff:
            if previous_tier == "archive":
                continue
            decisions.append(
                WidgetGraduationDecision(
                    widget_name=row.widget_name,
                    surface=row.surface,
                    tier="archive",
                    confidence=0.9,
                    interactions_in_window=0,
                    window_days=window_days,
                    previous_tier=previous_tier,
                    pocket_id=row.pocket_id,
                    scope=list(row.scope),
                    reason=f"Untouched for over {archive_days} days.",
                )
            )
            continue

        # Fade: seen in history but nothing promoting in the pin window.
        if row.promoting_count == 0 and last < window_cutoff:
            if previous_tier == "fade":
                continue
            decisions.append(
                WidgetGraduationDecision(
                    widget_name=row.widget_name,
                    surface=row.surface,
                    tier="fade",
                    confidence=0.6,
                    interactions_in_window=0,
                    window_days=window_days,
                    previous_tier=previous_tier,
                    pocket_id=row.pocket_id,
                    scope=list(row.scope),
                    reason="No promoting interactions in window.",
                )
            )

    return WidgetGraduationReport(
        decisions=decisions,
        scanned_widgets=len(usage),
        window_days=window_days,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Scan — co-occurrence. Turns the projection's signature-level counts
# into candidate widget proposals above the threshold.
# ---------------------------------------------------------------------------


def scan_for_cooccurrences(
    projection: WidgetProjection,
    *,
    threshold: int = DEFAULT_COOCCURRENCE_THRESHOLD,
    window_s: int = DEFAULT_SESSION_GAP_SECONDS,
    pocket_id: str | None = None,
    scope: str | None = None,
) -> CooccurrenceReport:
    """Return co-occurring widget pairs whose count crossed the
    threshold. Ports #942's scan over the (correct, dedup-safe)
    signatures the projection produces.

    ``threshold`` defaults to 3 per #942. Confidence is the same
    ``min(1.0, count / (threshold * 3))`` ramp #942 used — kept
    verbatim so existing UI copy doesn't drift.
    """

    pairs = projection.cooccurrences(min_count=threshold, pocket_id=pocket_id, limit=0)
    if scope:
        pairs = [p for p in pairs if scope in p.scope]

    candidates = [
        CooccurrenceCandidate(
            signature=p.signature,
            widget_a=p.widget_a,
            widget_b=p.widget_b,
            count=p.count,
            window_s=window_s,
            pocket_id=p.pocket_id,
            scope=list(p.scope),
            confidence=min(1.0, p.count / (threshold * 3)),
        )
        for p in pairs
    ]

    return CooccurrenceReport(
        candidates=candidates,
        scanned_pairs=len(pairs),
        threshold=threshold,
        window_s=window_s,
    )


# ---------------------------------------------------------------------------
# Apply — turns decisions into journal events.
# ---------------------------------------------------------------------------


async def apply_widget_graduations(
    decisions: list[WidgetGraduationDecision],
    store: WidgetJournalStore,
    *,
    scope: list[str],
    correlation_id: Any = None,
) -> list[WidgetGraduationDecision]:
    """Emit a ``widget.graduated`` event per decision. Returns the
    subset that completed without error — per-decision failures are
    logged and skipped so one broken emit doesn't block the others.
    """

    if not decisions:
        return []

    _require_scope(scope)

    applied: list[WidgetGraduationDecision] = []
    for decision in decisions:
        try:
            await store.log_widget_graduation(
                scope=scope,
                widget_name=decision.widget_name,
                surface=decision.surface,
                tier=decision.tier,
                confidence=decision.confidence,
                interactions_in_window=decision.interactions_in_window,
                window_days=decision.window_days,
                previous_tier=decision.previous_tier,
                pocket_id=decision.pocket_id,
                reason=decision.reason,
                correlation_id=correlation_id,
            )
        except Exception:
            logger.exception(
                "Widget graduation: journal emit failed for %s@%s",
                decision.widget_name,
                decision.surface,
            )
            continue
        applied.append(decision)
    return applied


async def apply_cooccurrences(
    candidates: list[CooccurrenceCandidate],
    store: WidgetJournalStore,
    *,
    scope: list[str],
    correlation_id: Any = None,
) -> list[CooccurrenceCandidate]:
    """Emit a ``widget.cooccurrence.detected`` event per candidate.

    The signature on the projection is already correct; this emit
    records an explicit snapshot of the threshold crossing so
    downstream consumers (dashboards, agent tools) can react without
    re-scanning on every read.
    """

    if not candidates:
        return []

    _require_scope(scope)

    applied: list[CooccurrenceCandidate] = []
    for candidate in candidates:
        try:
            await store.log_cooccurrence(
                scope=scope,
                widget_a=candidate.widget_a,
                widget_b=candidate.widget_b,
                count=candidate.count,
                window_s=candidate.window_s,
                pocket_id=candidate.pocket_id,
                correlation_id=correlation_id,
            )
        except Exception:
            logger.exception(
                "Widget co-occurrence: journal emit failed for %s",
                candidate.signature,
            )
            continue
        applied.append(candidate)
    return applied


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _require_scope(scope: list[str]) -> None:
    if not scope:
        raise ValueError(
            "apply_* requires a non-empty scope — the journal "
            "invariant refuses events with scope=[]."
        )


def _ensure_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts
