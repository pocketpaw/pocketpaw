# ee/retrieval/projection.py — In-memory projection over retrieval + graduation events.
# Created: 2026-04-16 (feat/retrieval-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Carries the read side of #936 (recent
# retrievals with filters) and #937 (graduation state per memory) as a
# replay over the org journal instead of a separate JSONL file. Same
# shape as ee/fabric/projection.py — ``rebuild(journal, since_seq)`` +
# incremental ``apply(entry)`` + filtered query methods.
#
# Two logical views live in one projection because they share the same
# event stream:
#   - RetrievalView: last-N retrievals, filterable by scope / correlation_id
#     / actor / pocket. This is what #936's GET /retrieval/log wanted.
#   - GraduationStateRow: one row per memory_id summarising the most-recent
#     graduation decision. This is what #937's scan wrote into a separate
#     JSONL. Rebuilt by folding every ``graduation.applied`` event.
#
# Keeping both in one projection means one replay pass does both, which
# matches how soul-protocol exposes the journal today (``replay_from``
# iterates the whole stream; ``query(action=...)`` filters by exact
# match, no globs — see the task constraint).

from __future__ import annotations

import logging
from bisect import insort
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from soul_protocol.engine.journal import Journal
from soul_protocol.spec.journal import EventEntry

from ee.fabric.policy import filter_visible
from ee.retrieval.events import (
    ACTION_GRADUATION_APPLIED,
    ACTION_RETRIEVAL_QUERY,
    ALL_RETRIEVAL_ACTIONS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public row shapes — stable dataclasses the router serialises. Kept as
# dataclasses rather than Pydantic so the projection has no runtime import
# cost from the model machinery on hot paths.
# ---------------------------------------------------------------------------


@dataclass
class RetrievalView:
    """One projected retrieval — a single ``retrieval.query`` event after replay.

    ``scope`` and ``correlation_id`` are lifted off the EventEntry so the
    view carries everything the router needs without re-reading the
    journal. ``actor_id`` flattens the ``Actor`` to a string for JSON.
    """

    request_id: str
    query: str
    actor_id: str
    actor_kind: str
    scope: list[str]
    correlation_id: str | None
    ts: datetime
    strategy: str
    sources_queried: list[str]
    sources_failed: list[dict[str, Any]]
    candidate_count: int
    candidates: list[dict[str, Any]]
    picked: list[str]
    latency_ms: int
    pocket_id: str | None
    trace_id: str | None
    seq: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "query": self.query,
            "actor_id": self.actor_id,
            "actor_kind": self.actor_kind,
            "scope": list(self.scope),
            "correlation_id": self.correlation_id,
            "ts": self.ts.isoformat(),
            "strategy": self.strategy,
            "sources_queried": list(self.sources_queried),
            "sources_failed": list(self.sources_failed),
            "candidate_count": self.candidate_count,
            "candidates": list(self.candidates),
            "picked": list(self.picked),
            "latency_ms": self.latency_ms,
            "pocket_id": self.pocket_id,
            "trace_id": self.trace_id,
            "seq": self.seq,
        }


@dataclass
class GraduationStateRow:
    """Most-recent graduation decision for one memory_id.

    The projection keeps only the latest tier per memory — a repeat
    graduation overwrites the row. The full history is still on the
    journal for anyone who needs the audit trail (``journal.query(action=
    "graduation.applied", correlation_id=...)``).
    """

    memory_id: str
    current_tier: str
    previous_tier: str | None
    kind: str
    access_count: int
    window_days: int
    pocket_id: str | None
    scope: list[str]
    reason: str
    applied_at: datetime
    seq: int


# ---------------------------------------------------------------------------
# Internal storage dataclasses — not exposed.
# ---------------------------------------------------------------------------


@dataclass
class _RetrievalRow:
    """Internal projection row for retrievals.

    Retrievals are kept in insertion order; the list is cheap to slice for
    "recent N" queries. A sorted-by-seq insert is used so out-of-order
    replays (unusual but possible on a merged journal) still land in seq
    order in the view.
    """

    view: RetrievalView

    def __lt__(self, other: _RetrievalRow) -> bool:
        return self.view.seq < other.view.seq


class RetrievalProjection:
    """Rebuilds + serves read views for retrieval + graduation events.

    One instance per process; rebuild is O(events) so operators can drop
    and rebuild if they suspect drift. No persistence — the projection is
    a pure fold over the journal.
    """

    def __init__(self, *, max_retrievals: int = 10_000) -> None:
        self._retrievals: list[_RetrievalRow] = []
        self._graduation: dict[str, GraduationStateRow] = {}
        self._cursor: int = 0
        # Soft cap — stops a busy org's projection from eating a lot of
        # RAM. Once the cap is hit we evict oldest-first. Callers who want
        # full history should read the journal directly.
        self._max = max_retrievals

    # -- Build / rebuild ----------------------------------------------------

    def rebuild(self, journal: Journal, *, since_seq: int = 0) -> int:
        """Replay the journal from ``since_seq`` (0 = genesis), applying
        every retrieval + graduation event. Returns the number of events
        applied. When ``since_seq == 0`` the projection wipes its state
        first so the rebuild is a true reset.
        """

        if since_seq == 0:
            self._retrievals.clear()
            self._graduation.clear()
            self._cursor = 0

        applied = 0
        for entry in journal.replay_from(since_seq):
            if entry.action not in ALL_RETRIEVAL_ACTIONS:
                continue
            self.apply(entry)
            applied += 1
        return applied

    # -- Incremental apply --------------------------------------------------

    def apply(self, entry: EventEntry) -> None:
        """Fold a single event into the projection."""

        if entry.action not in ALL_RETRIEVAL_ACTIONS:
            return

        payload: dict[str, Any] = dict(entry.payload) if isinstance(entry.payload, dict) else {}
        seq = getattr(entry, "seq", None) or 0
        if seq > self._cursor:
            self._cursor = seq

        if entry.action == ACTION_RETRIEVAL_QUERY:
            self._apply_retrieval(entry, payload, seq)
        elif entry.action == ACTION_GRADUATION_APPLIED:
            self._apply_graduation(entry, payload, seq)

    def _apply_retrieval(
        self,
        entry: EventEntry,
        payload: dict[str, Any],
        seq: int,
    ) -> None:
        view = RetrievalView(
            request_id=str(payload.get("request_id") or entry.id),
            query=str(payload.get("query", "")),
            actor_id=str(entry.actor.id),
            actor_kind=str(entry.actor.kind),
            scope=list(entry.scope),
            correlation_id=_uuid_to_str(entry.correlation_id),
            ts=_as_datetime(entry.ts),
            strategy=str(payload.get("strategy", "")),
            sources_queried=list(payload.get("sources_queried") or []),
            sources_failed=list(payload.get("sources_failed") or []),
            candidate_count=int(payload.get("candidate_count", 0) or 0),
            candidates=list(payload.get("candidates") or []),
            picked=list(payload.get("picked") or []),
            latency_ms=int(payload.get("latency_ms", 0) or 0),
            pocket_id=_none_or_str(payload.get("pocket_id")),
            trace_id=_none_or_str(payload.get("trace_id")),
            seq=seq,
        )
        insort(self._retrievals, _RetrievalRow(view=view))
        # Evict oldest once we pass the cap — keep the tail (newest).
        if len(self._retrievals) > self._max:
            overflow = len(self._retrievals) - self._max
            del self._retrievals[:overflow]

    def _apply_graduation(
        self,
        entry: EventEntry,
        payload: dict[str, Any],
        seq: int,
    ) -> None:
        memory_id = payload.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id:
            logger.warning("Retrieval projection: graduation event missing memory_id")
            return
        row = GraduationStateRow(
            memory_id=memory_id,
            current_tier=str(payload.get("to_tier", "")),
            previous_tier=_none_or_str(payload.get("from_tier")),
            kind=str(payload.get("kind", "")),
            access_count=int(payload.get("access_count", 0) or 0),
            window_days=int(payload.get("window_days", 0) or 0),
            pocket_id=_none_or_str(payload.get("pocket_id")),
            scope=list(entry.scope),
            reason=str(payload.get("reason", "")),
            applied_at=_as_datetime(entry.ts),
            seq=seq,
        )
        self._graduation[memory_id] = row

    # -- Retrieval queries --------------------------------------------------

    def recent_retrievals(
        self,
        *,
        scope: str | None = None,
        actor_id: str | None = None,
        pocket_id: str | None = None,
        limit: int = 20,
        requester_scopes: list[str] | None = None,
    ) -> list[RetrievalView]:
        """Return the most-recent retrievals, newest-first, with optional
        filters. Scope filtering runs via ee.fabric.policy.filter_visible so
        the containment rules (``org:*`` matches ``org:sales`` and vice
        versa) stay identical to Fabric's — no divergent semantics between
        the two projections.
        """

        rows = [row.view for row in self._retrievals]

        if scope:
            rows = [r for r in rows if scope in r.scope]
        if actor_id:
            rows = [r for r in rows if r.actor_id == actor_id]
        if pocket_id:
            rows = [r for r in rows if r.pocket_id == pocket_id]

        if requester_scopes:
            visible, _hidden = filter_visible(rows, requester_scopes)
            rows = list(visible)

        # Newest first, cap at limit.
        rows.sort(key=lambda v: v.seq, reverse=True)
        if limit > 0:
            rows = rows[:limit]
        return rows

    def retrievals_by_correlation(
        self,
        correlation_id: str,
        *,
        requester_scopes: list[str] | None = None,
    ) -> list[RetrievalView]:
        """All retrievals sharing one correlation_id — the "session" view.

        Ordered oldest-first so a UI can render a chronological trail of
        what the agent asked during one run.
        """

        rows = [row.view for row in self._retrievals if row.view.correlation_id == correlation_id]
        if requester_scopes:
            visible, _hidden = filter_visible(rows, requester_scopes)
            rows = list(visible)
        rows.sort(key=lambda v: v.seq)
        return rows

    # -- Graduation queries --------------------------------------------------

    def graduation_state(
        self,
        *,
        memory_id: str | None = None,
        requester_scopes: list[str] | None = None,
    ) -> list[GraduationStateRow]:
        """Current graduation state — one row per memory_id.

        Passing ``memory_id`` returns a single-row list (or empty) for the
        common "what tier is this memory at?" probe.
        """

        if memory_id is not None:
            row = self._graduation.get(memory_id)
            rows = [row] if row else []
        else:
            rows = list(self._graduation.values())

        if requester_scopes:
            visible, _hidden = filter_visible(rows, requester_scopes)
            rows = list(visible)

        rows.sort(key=lambda r: r.seq, reverse=True)
        return rows

    # -- Diagnostics --------------------------------------------------------

    @property
    def cursor(self) -> int:
        """Latest seq the projection has seen. Persist this to skip ahead
        on restart.
        """

        return self._cursor

    def size(self) -> dict[str, int]:
        """Quick counters for the /retrieval/stats endpoint."""

        return {
            "retrievals": len(self._retrievals),
            "graduations": len(self._graduation),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _as_datetime(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return datetime.now()


def _uuid_to_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    try:
        return str(value)
    except Exception:  # noqa: BLE001 — defensive only.
        return None


def _none_or_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and not value:
        return None
    return str(value)
