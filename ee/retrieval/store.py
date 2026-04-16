# ee/retrieval/store.py — Journal-backed write path for retrieval + graduation.
# Created: 2026-04-16 (feat/retrieval-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Replaces the JSONL sink from #936 (file at
# ``~/.pocketpaw/retrieval.jsonl`` with an asyncio.Lock around writes) and
# the apply-side of #937's graduation policy (which wrote decisions into
# the same JSONL). Both land on the org journal now — one append-only log
# instead of a side-channel file, and write serialization is inherited
# from SQLite's WAL + transaction semantics rather than from a per-
# process asyncio lock that didn't protect against multi-process anyway.
#
# The store is small on purpose. It:
#   - emits ``retrieval.query`` events when a retrieval happens
#   - emits ``graduation.applied`` events when a graduation decision fires
#   - folds each emitted event into a shared RetrievalProjection so
#     reads are consistent without waiting for a rebuild
#
# Everything else (policy decisions, REST surface, soul mutations) lives
# in policy.py / router.py so the store has no dependencies on the UI
# layer or on soul-protocol beyond the journal primitive.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from soul_protocol.engine.journal import Journal
from soul_protocol.spec.journal import Actor, EventEntry

from ee.retrieval.events import (
    ACTION_GRADUATION_APPLIED,
    ACTION_RETRIEVAL_QUERY,
    graduation_applied_payload,
    retrieval_query_payload,
)
from ee.retrieval.projection import RetrievalProjection

_SYSTEM_RETRIEVAL_ACTOR_ID = "system:retrieval"
_SYSTEM_GRADUATION_ACTOR_ID = "system:graduation"


class RetrievalJournalStore:
    """Journal-backed emitter for retrieval + graduation events.

    Wiring:

        from ee.journal_dep import get_journal
        journal = get_journal()
        store = RetrievalJournalStore(journal)
        store.bootstrap()
        await store.log_retrieval(request, result, actor=..., scope=["org:sales"])
    """

    def __init__(
        self,
        journal: Journal,
        *,
        projection: RetrievalProjection | None = None,
        default_retrieval_actor: Actor | None = None,
        default_graduation_actor: Actor | None = None,
    ) -> None:
        self._journal = journal
        self._projection = projection or RetrievalProjection()
        self._default_retrieval_actor = default_retrieval_actor or Actor(
            kind="system",
            id=_SYSTEM_RETRIEVAL_ACTOR_ID,
            scope_context=[],
        )
        self._default_graduation_actor = default_graduation_actor or Actor(
            kind="system",
            id=_SYSTEM_GRADUATION_ACTOR_ID,
            scope_context=[],
        )

    # -- Bootstrap ----------------------------------------------------------

    def bootstrap(self, *, since_seq: int = 0) -> int:
        """Warm the projection from the journal. Returns the number of
        events applied. Call once at process start.
        """

        return self._projection.rebuild(self._journal, since_seq=since_seq)

    @property
    def projection(self) -> RetrievalProjection:
        return self._projection

    # -- Writes -------------------------------------------------------------

    async def log_retrieval(
        self,
        *,
        scope: list[str],
        query: str,
        request_id: str | None = None,
        strategy: str = "parallel",
        sources_queried: list[str] | None = None,
        sources_failed: list[dict[str, Any]] | None = None,
        candidates: list[dict[str, Any]] | None = None,
        picked: list[str] | None = None,
        latency_ms: int = 0,
        pocket_id: str | None = None,
        trace_id: str | None = None,
        actor: Actor | None = None,
        correlation_id: UUID | None = None,
    ) -> EventEntry:
        """Emit one ``retrieval.query`` event and fold it into the projection.

        ``scope`` is required — the journal's EventEntry invariant rejects
        an empty scope list. Callers that don't have a scope for their
        retrieval (rare; the retrieval router always knows one) should
        make that explicit at the call site rather than having the store
        fabricate an empty list.
        """

        _require_scope(scope)

        payload = retrieval_query_payload(
            request_id=request_id or str(uuid4()),
            query=query,
            strategy=strategy,
            sources_queried=sources_queried,
            sources_failed=sources_failed,
            candidates=candidates,
            picked=picked,
            latency_ms=latency_ms,
            pocket_id=pocket_id,
            trace_id=trace_id,
        )
        entry = self._build_entry(
            action=ACTION_RETRIEVAL_QUERY,
            scope=scope,
            actor=actor or self._default_retrieval_actor,
            correlation_id=correlation_id,
            payload=payload,
        )
        self._journal.append(entry)
        self._projection.apply(entry)
        return entry

    async def log_graduation(
        self,
        *,
        scope: list[str],
        memory_id: str,
        kind: str,
        access_count: int,
        window_days: int,
        from_tier: str | None,
        to_tier: str,
        pocket_id: str | None = None,
        reason: str = "",
        actor: Actor | None = None,
        correlation_id: UUID | None = None,
    ) -> EventEntry:
        """Emit one ``graduation.applied`` event + fold it into the projection.

        Graduation decisions are one-event-per-promotion. Replaying the
        stream gives you the full history; the projection keeps only the
        most-recent decision per memory_id (see
        RetrievalProjection._apply_graduation).
        """

        _require_scope(scope)

        payload = graduation_applied_payload(
            memory_id=memory_id,
            kind=kind,
            access_count=access_count,
            window_days=window_days,
            from_tier=from_tier,
            to_tier=to_tier,
            pocket_id=pocket_id,
            reason=reason,
        )
        entry = self._build_entry(
            action=ACTION_GRADUATION_APPLIED,
            scope=scope,
            actor=actor or self._default_graduation_actor,
            correlation_id=correlation_id,
            payload=payload,
        )
        self._journal.append(entry)
        self._projection.apply(entry)
        return entry

    # -- Internals ----------------------------------------------------------

    def _build_entry(
        self,
        *,
        action: str,
        scope: list[str],
        actor: Actor,
        correlation_id: UUID | None,
        payload: dict[str, Any],
    ) -> EventEntry:
        return EventEntry(
            id=uuid4(),
            ts=datetime.now(UTC),
            actor=actor,
            action=action,
            scope=list(scope),
            correlation_id=correlation_id,
            payload=payload,
        )


def _require_scope(scope: list[str]) -> None:
    if not scope:
        raise ValueError(
            "RetrievalJournalStore requires a non-empty scope on every write — "
            "the journal invariant refuses events with scope=[]."
        )
