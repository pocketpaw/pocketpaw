# ee/retrieval/policy.py — Graduation policy over the journal-backed projection.
# Created: 2026-04-16 (feat/retrieval-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Ports #937's access-count graduation logic
# from a JSONL scan onto a projection scan. Every numeric threshold and
# decision rule from #937 is preserved verbatim — only the input source
# changes (projection rows instead of RetrievalLogEntry rows) and the
# output emission goes through ``RetrievalJournalStore.log_graduation``
# instead of mutating a JSONL file.
#
# Why keep #937's rules verbatim? Because the thresholds were tuned for
# feel ("accessed 10x in a month = episodic graduates to semantic") and
# the refactor isn't the place to re-tune. A follow-up slice can make
# them configurable per pocket.

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ee.retrieval.projection import RetrievalProjection
from ee.retrieval.store import RetrievalJournalStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tuning defaults — carried verbatim from #937 so the runtime behaviour
# stays identical after the refactor. Override via scan_for_graduations
# kwargs when a pocket needs a different cadence.
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_DAYS = 30
DEFAULT_EPISODIC_THRESHOLD = 10
DEFAULT_SEMANTIC_THRESHOLD = 50


GraduationKind = Literal[
    "episodic_to_semantic",
    "semantic_to_core",
    "promote_procedural",
]


@dataclass
class GraduationDecision:
    """One proposed tier change. The scan emits these; the apply path turns
    them into ``graduation.applied`` events on the journal.

    Mirrors the shape from #937 so downstream consumers (widget
    graduation, soul-protocol's own memory.graduated listeners) don't
    need to learn a new dataclass.
    """

    memory_id: str
    kind: GraduationKind
    access_count: int
    window_days: int
    from_tier: str | None
    to_tier: str
    actor: str = ""
    pocket_id: str | None = None
    reason: str = ""

    def short(self) -> str:
        """One-line summary for terminal output / operator dashboards."""

        from_label = self.from_tier or "?"
        return (
            f"[{self.kind}] {self.memory_id} {from_label}->{self.to_tier} "
            f"({self.access_count} accesses in {self.window_days}d)"
        )


@dataclass
class GraduationReport:
    """Output of one scan — decisions + scan metadata."""

    decisions: list[GraduationDecision] = field(default_factory=list)
    scanned_retrievals: int = 0
    window_days: int = DEFAULT_WINDOW_DAYS
    dry_run: bool = True
    generated_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Scan — counts per-memory accesses off the projection and emits decisions.
# ---------------------------------------------------------------------------


def scan_for_graduations(
    projection: RetrievalProjection,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    episodic_threshold: int = DEFAULT_EPISODIC_THRESHOLD,
    semantic_threshold: int = DEFAULT_SEMANTIC_THRESHOLD,
    actor_id: str | None = None,
    pocket_id: str | None = None,
    scope: str | None = None,
    dry_run: bool = True,
) -> GraduationReport:
    """Walk the projection's retrievals in the last ``window_days`` and
    return a report of memories that crossed an access threshold.

    ``actor_id`` / ``pocket_id`` / ``scope`` narrow the scan the same way
    #937's scan accepted filters — so an operator can ask "which memories
    have been accessed enough by user:priya in pocket-1 to graduate?"
    without scanning the whole org's history.

    This path never writes; apply() does. Matches #937's original dry-
    run-by-default contract so an operator can review the report before
    committing the tier change.
    """

    # EventEntry.ts is tz-aware UTC per the journal spec, so compute the
    # cutoff in UTC too. Using a naive datetime here would TypeError on
    # the comparison below the first time a real journal entry flows in.
    since = datetime.now(UTC) - timedelta(days=window_days)
    rows = projection.recent_retrievals(
        scope=scope,
        actor_id=actor_id,
        pocket_id=pocket_id,
        limit=0,  # 0 == all, see projection.recent_retrievals limit contract
    )
    # recent_retrievals returns newest-first; cap by time window here so the
    # projection doesn't have to grow a since= filter. Defensive: coerce
    # naive ts values to UTC so the comparison stays well-defined when a
    # shim adapter emits a naive datetime.
    rows = [r for r in rows if _ensure_aware(r.ts) >= since]

    counts: Counter[str] = Counter()
    contexts: dict[str, dict[str, Any]] = {}

    for view in rows:
        for candidate in view.candidates:
            if not isinstance(candidate, dict):
                continue
            mid = candidate.get("id")
            if not isinstance(mid, str) or not mid:
                continue
            counts[mid] += 1
            ctx = contexts.setdefault(
                mid,
                {
                    "tier": candidate.get("tier"),
                    "actor": view.actor_id,
                    "pocket_id": view.pocket_id,
                },
            )
            if candidate.get("tier"):
                ctx["tier"] = candidate["tier"]

    decisions: list[GraduationDecision] = []
    for mid, count in counts.most_common():
        ctx = contexts.get(mid, {})
        decision = _decide(
            memory_id=mid,
            count=count,
            from_tier=ctx.get("tier"),
            actor=ctx.get("actor", "") or "",
            pocket_id=ctx.get("pocket_id"),
            episodic_threshold=episodic_threshold,
            semantic_threshold=semantic_threshold,
            window_days=window_days,
        )
        if decision is not None:
            decisions.append(decision)

    return GraduationReport(
        decisions=decisions,
        scanned_retrievals=len(rows),
        window_days=window_days,
        dry_run=dry_run,
    )


def _decide(
    *,
    memory_id: str,
    count: int,
    from_tier: str | None,
    actor: str,
    pocket_id: str | None,
    episodic_threshold: int,
    semantic_threshold: int,
    window_days: int,
) -> GraduationDecision | None:
    """Threshold check — ported verbatim from #937.

    Empty tier is treated as episodic because soul-protocol defaults
    interaction-derived memories to episodic and the retrieval log
    doesn't always carry tier on every candidate.
    """

    tier = (from_tier or "").lower()

    if tier in {"episodic", ""} and count >= episodic_threshold:
        return GraduationDecision(
            memory_id=memory_id,
            actor=actor,
            pocket_id=pocket_id,
            kind="episodic_to_semantic",
            access_count=count,
            window_days=window_days,
            from_tier=from_tier or "episodic",
            to_tier="semantic",
            reason=(
                f"Accessed {count}x in last {window_days} days (threshold {episodic_threshold})."
            ),
        )

    if tier == "semantic" and count >= semantic_threshold:
        return GraduationDecision(
            memory_id=memory_id,
            actor=actor,
            pocket_id=pocket_id,
            kind="semantic_to_core",
            access_count=count,
            window_days=window_days,
            from_tier="semantic",
            to_tier="core",
            reason=(
                f"Accessed {count}x in last {window_days} days (threshold {semantic_threshold})."
            ),
        )

    return None


# ---------------------------------------------------------------------------
# Apply — turns GraduationDecisions into graduation.applied events + fires
# the optional soul.remember path from #937.
# ---------------------------------------------------------------------------


async def apply_decisions(
    decisions: list[GraduationDecision],
    store: RetrievalJournalStore,
    *,
    scope: list[str],
    soul: Any = None,
    correlation_id: Any = None,
) -> list[GraduationDecision]:
    """Emit a ``graduation.applied`` event for each decision, optionally
    mutating the soul. Returns the subset that completed without error.

    This is the journal-backed replacement for #937's ``apply_decisions``.
    The soul-side mutation is still best-effort — graduation must never
    break the runtime, so per-decision failures are logged and skipped.
    The journal event is written regardless of whether soul.remember
    succeeded; operators can retry the soul step separately without
    double-counting the journal entry.
    """

    if not decisions:
        return []

    _require_scope(scope)

    applied: list[GraduationDecision] = []
    for decision in decisions:
        try:
            await store.log_graduation(
                scope=scope,
                memory_id=decision.memory_id,
                kind=decision.kind,
                access_count=decision.access_count,
                window_days=decision.window_days,
                from_tier=decision.from_tier,
                to_tier=decision.to_tier,
                pocket_id=decision.pocket_id,
                reason=decision.reason,
                correlation_id=correlation_id,
            )
        except Exception:
            logger.exception("Graduation apply: journal emit failed for %s", decision.memory_id)
            continue

        if soul is not None and hasattr(soul, "remember") and hasattr(soul, "recall"):
            try:
                await _mutate_soul(soul, decision)
            except Exception:
                logger.exception(
                    "Graduation apply: soul mutation failed for %s", decision.memory_id
                )
                # Journal event already written — don't drop the decision
                # from applied() just because the soul side failed. The
                # journal is the source of truth; the soul copy is a cache.

        applied.append(decision)
    return applied


async def _mutate_soul(soul: Any, decision: GraduationDecision) -> None:
    """Mirror #937's soul.remember() call so the in-memory soul reflects
    the new tier. Kept as a separate helper so apply_decisions() can
    short-circuit on import / attribute gaps without nesting try/except.
    """

    content = await _lookup_memory_content(soul, decision.memory_id)
    if not content:
        logger.debug("Graduation apply: memory %s not found in soul", decision.memory_id)
        return

    target_type = _resolve_tier(decision.to_tier)
    await soul.remember(
        content=f"[graduated:{decision.kind}] {content}",
        type=target_type,
        importance=8 if decision.to_tier == "core" else 7,
    )


def _resolve_tier(tier: str):
    """Translate a tier name to soul-protocol's MemoryType enum, falling
    back to the raw string when soul-protocol isn't importable (common in
    test contexts that mock the soul interface).
    """

    try:
        from soul_protocol.runtime.types import MemoryType
    except ImportError:
        return tier

    try:
        return MemoryType(tier)
    except ValueError:
        return MemoryType.SEMANTIC


async def _lookup_memory_content(soul: Any, memory_id: str) -> str:
    """Best-effort lookup — soul-protocol doesn't expose get-by-id on the
    soul manager yet. Pull a wide recall and match by id. Identical to the
    #937 helper.
    """

    try:
        memories = await soul.recall("", limit=500)
    except Exception:
        return ""
    for entry in memories:
        if getattr(entry, "id", None) == memory_id:
            return getattr(entry, "content", "")
    return ""


def _require_scope(scope: list[str]) -> None:
    if not scope:
        raise ValueError(
            "apply_decisions requires a non-empty scope — the journal "
            "invariant refuses events with scope=[]."
        )


def _ensure_aware(ts: datetime) -> datetime:
    """Promote a naive datetime to UTC for comparison.

    The journal spec always emits tz-aware ``EventEntry.ts`` values, but
    the projection's defensive parser can fall back to ``datetime.now()``
    (naive) when a malformed ts slips in. Treat those as UTC rather than
    crashing the scan.
    """

    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts
