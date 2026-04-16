# ee/fabric/journal_store.py — Journal-backed write path for Fabric objects.
# Created: 2026-04-16 (feat/fabric-journal-projection) — Wave 3 / Org Architecture RFC,
# Phase 3. Replaces the scope-filtering slice of #938, which had tried to bolt scope
# onto the legacy SQLite FabricStore and hit two blockers (schema migration; pagination
# leak). By writing to the org journal instead of a separate SQLite file, both blockers
# vanish by construction: the journal is append-only (no schema migrations), and the
# read path is a projection that applies scope filters BEFORE computing totals.
#
# This store is deliberately narrow — it handles object lifecycle only (create /
# update / archive / query). Object-type definitions and object-to-object links still
# live in the legacy ee/fabric/store.py::FabricStore. Those stay SQLite-backed until a
# follow-up slice; types are low-churn config rather than per-tenant data, and links
# need a richer projection model than one event fold. Callers can hold both: legacy
# FabricStore for schema + links, FabricJournalStore for objects + scope filtering.
#
# The store owns a FabricProjection instance — a read-through cache in front of the
# journal. Reads are served from the projection; writes go to the journal and the
# projection applies the event immediately so the next read sees the change without
# a full rebuild. On process start, bootstrap() replays from genesis to warm the
# projection; operators who persist a cursor can skip ahead.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from soul_protocol.engine.journal import Journal
from soul_protocol.spec.journal import Actor, EventEntry

from ee.fabric.events import (
    ACTION_OBJECT_ARCHIVED,
    ACTION_OBJECT_CREATED,
    ACTION_OBJECT_UPDATED,
    object_archived_payload,
    object_created_payload,
    object_updated_payload,
)
from ee.fabric.models import FabricObject, FabricQuery, FabricQueryResult
from ee.fabric.projection import FabricProjection

_SYSTEM_ACTOR_ID = "system:fabric"


class FabricJournalStore:
    """Journal-backed CRUD + query for Fabric objects.

    The store is event-sourced: every write becomes an EventEntry on the
    org journal, and reads are served from an in-memory FabricProjection.
    Writes also fold the emitted event into the projection so reads are
    consistent without waiting for a periodic rebuild.

    Typical wiring:

        from ee.journal_dep import get_journal
        journal = get_journal()
        store = FabricJournalStore(journal)
        store.bootstrap()
        await store.create(...)
    """

    def __init__(
        self,
        journal: Journal,
        *,
        projection: FabricProjection | None = None,
        default_actor: Actor | None = None,
    ) -> None:
        self._journal = journal
        self._projection = projection or FabricProjection()
        self._default_actor = default_actor or Actor(
            kind="system",
            id=_SYSTEM_ACTOR_ID,
            scope_context=[],
        )

    # -- Bootstrap ----------------------------------------------------------

    def bootstrap(self, *, since_seq: int = 0) -> int:
        """Warm the projection from the journal. Returns the number of
        events applied. Call once at process start; callers that persist
        a cursor can pass ``since_seq`` to skip already-applied events.
        """

        return self._projection.rebuild(self._journal, since_seq=since_seq)

    @property
    def projection(self) -> FabricProjection:
        """Expose the projection for diagnostics + tests. Not part of
        the stable API — if you're reaching for this in production code,
        add a real method here instead.
        """

        return self._projection

    # -- Writes -------------------------------------------------------------

    async def create(
        self,
        obj: FabricObject,
        *,
        scope: list[str],
        actor: Actor | None = None,
        correlation_id: UUID | None = None,
    ) -> FabricObject:
        """Append a ``fabric.object.created`` event and return the object
        as the projection now sees it.

        ``scope`` is required — the journal's EventEntry invariant demands
        a non-empty scope list, and callers that don't supply one should
        make that explicit rather than having the store fabricate one.
        """

        _require_scope(scope)

        payload = object_created_payload(
            object_id=obj.id,
            type_id=obj.type_id,
            type_name=obj.type_name,
            properties=obj.properties,
            source_connector=obj.source_connector,
            source_id=obj.source_id,
        )
        entry = self._build_entry(
            action=ACTION_OBJECT_CREATED,
            scope=scope,
            actor=actor,
            correlation_id=correlation_id,
            payload=payload,
        )
        self._journal.append(entry)
        self._projection.apply(entry)

        projected = self._projection.query(
            FabricQuery(type_id=obj.type_id, limit=10000),
            requester_scopes=None,
        )
        for candidate in projected.objects:
            if candidate.id == obj.id:
                return candidate
        # Fallback — should not happen because we just applied the event,
        # but preserve the caller's view if the projection disagrees.
        return obj

    async def update(
        self,
        object_id: str,
        properties: dict[str, Any],
        *,
        scope: list[str],
        actor: Actor | None = None,
        correlation_id: UUID | None = None,
    ) -> FabricObject | None:
        """Append a ``fabric.object.updated`` event. Returns the updated
        object as the projection now sees it, or None if the object is
        unknown.
        """

        _require_scope(scope)

        payload = object_updated_payload(
            object_id=object_id,
            properties=properties,
        )
        entry = self._build_entry(
            action=ACTION_OBJECT_UPDATED,
            scope=scope,
            actor=actor,
            correlation_id=correlation_id,
            payload=payload,
        )
        self._journal.append(entry)
        self._projection.apply(entry)
        return self._lookup(object_id)

    async def archive(
        self,
        object_id: str,
        *,
        scope: list[str],
        reason: str = "",
        actor: Actor | None = None,
        correlation_id: UUID | None = None,
    ) -> bool:
        """Append a ``fabric.object.archived`` event. Returns True when
        the archive was applied, False when the object was unknown.

        Archive is an event, not a delete — the journal preserves history
        and the projection hides archived objects from query() without
        actually removing them. Audit queries can still walk them.
        """

        _require_scope(scope)

        payload = object_archived_payload(object_id=object_id, reason=reason)
        entry = self._build_entry(
            action=ACTION_OBJECT_ARCHIVED,
            scope=scope,
            actor=actor,
            correlation_id=correlation_id,
            payload=payload,
        )
        self._journal.append(entry)
        self._projection.apply(entry)
        return self._lookup(object_id) is None

    # -- Reads --------------------------------------------------------------

    async def query(
        self,
        q: FabricQuery,
        *,
        requester_scopes: list[str] | None = None,
    ) -> FabricQueryResult:
        """Run a query against the projection with the caller's scope
        applied. ``requester_scopes=None`` or ``[]`` returns everything
        (admin / system path).
        """

        return self._projection.query(q, requester_scopes=requester_scopes)

    async def get(
        self,
        object_id: str,
        *,
        requester_scopes: list[str] | None = None,
    ) -> FabricObject | None:
        """Return the current projection of a single object, or None when
        the caller's scope doesn't grant access (indistinguishable from
        not-found — intentional so scope filtering can't be used as a
        probe for hidden records).
        """

        result = await self.query(
            FabricQuery(limit=10000),
            requester_scopes=requester_scopes,
        )
        for obj in result.objects:
            if obj.id == object_id:
                return obj
        return None

    # -- Internals ----------------------------------------------------------

    def _build_entry(
        self,
        *,
        action: str,
        scope: list[str],
        actor: Actor | None,
        correlation_id: UUID | None,
        payload: dict[str, Any],
    ) -> EventEntry:
        return EventEntry(
            id=uuid4(),
            ts=datetime.now(UTC),
            actor=actor or self._default_actor,
            action=action,
            scope=list(scope),
            correlation_id=correlation_id,
            payload=payload,
        )

    def _lookup(self, object_id: str) -> FabricObject | None:
        """Pull one object out of the projection regardless of scope — for
        internal read-after-write confirmation only. Public callers go
        through query() / get() which apply scope.
        """

        result = self._projection.query(FabricQuery(limit=10000), requester_scopes=None)
        for obj in result.objects:
            if obj.id == object_id:
                return obj
        return None


def _require_scope(scope: list[str]) -> None:
    """Raise early if the caller forgot to pass a scope. Matches the
    journal's own EventEntry invariant (min_length=1 on scope) but fires
    before we build the entry so the error message points at the Fabric
    API, not at a pydantic validation error deep inside soul-protocol.
    """

    if not scope:
        raise ValueError(
            "FabricJournalStore requires a non-empty scope on every write — "
            "the journal invariant refuses events with scope=[]."
        )
