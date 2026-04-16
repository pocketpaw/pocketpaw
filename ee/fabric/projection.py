# ee/fabric/projection.py — In-memory projection over Fabric journal events.
# Created: 2026-04-16 (feat/fabric-journal-projection) — Wave 3 / Org Architecture RFC,
# Phase 3. The projection is the read path: it replays `fabric.object.*` events off
# the org journal to reconstruct current-object state in memory, then serves queries
# against that state with scope filtering applied BEFORE the total count is computed.
#
# That last sentence is load-bearing. #938 attempted scope filtering in a SQLite
# FabricStore and ran into the pagination leak: `total` was computed pre-filter,
# `objects` was returned post-filter, so a caller could detect "hidden" objects exist
# by spotting a mismatch. Doing the filter in the projection means `total` is always
# derived from the filtered set — no pre-filter count is ever exposed.
#
# This projection is intentionally tiny:
#   - rebuild(journal, since_seq=0): replay events, build the state dict
#   - apply(entry): incremental single-event update
#   - query(...): return a FabricQueryResult scoped to the caller
# No persistence layer, no caching beyond the in-memory dict. Reopen a journal,
# rebuild(), and you're back in sync — same guarantee as a good CQRS read model.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from soul_protocol.engine.journal import Journal
from soul_protocol.spec.journal import EventEntry

from ee.fabric.events import (
    ACTION_OBJECT_ARCHIVED,
    ACTION_OBJECT_CREATED,
    ACTION_OBJECT_UPDATED,
    ALL_FABRIC_ACTIONS,
)
from ee.fabric.models import FabricObject, FabricQuery, FabricQueryResult
from ee.fabric.policy import filter_visible

logger = logging.getLogger(__name__)


@dataclass
class _ProjectedObject:
    """Internal projection row — what the replay loop maintains.

    We keep this separate from FabricObject so the replay loop can hold on
    to the event's scope list (which lives on the EventEntry, not the
    payload) without inventing a new model field.
    """

    obj: FabricObject
    scope: list[str] = field(default_factory=list)
    archived: bool = False
    last_seq: int = 0

    def as_public(self) -> FabricObject:
        """Return a FabricObject with the scope injected as a transient
        attribute so the policy engine can read it via its duck-typed
        `scope` lookup. The underlying model doesn't persist scope on
        the object row itself — it's owned by the journal.
        """

        payload = self.obj.model_copy(deep=True)
        # The policy engine reads `scope` off the entity; attaching it as a
        # Python attribute (not a model field) keeps the FabricObject model
        # stable while still letting visible() / filter_visible() see it.
        object.__setattr__(payload, "scope", list(self.scope))
        return payload


class FabricProjection:
    """Rebuilds and maintains a current-state view of Fabric objects from
    the org journal. One instance per process is the usual pattern; the
    projection is cheap to rebuild (O(events)) so operators can drop and
    rebuild at will if they suspect drift.
    """

    def __init__(self) -> None:
        self._objects: dict[str, _ProjectedObject] = {}
        self._cursor: int = 0

    # -- Build / rebuild ----------------------------------------------------

    def rebuild(self, journal: Journal, *, since_seq: int = 0) -> int:
        """Replay `fabric.object.*` events from the journal starting at
        ``since_seq`` (0 = from genesis). Returns the number of events
        applied.

        When ``since_seq`` is 0 the projection wipes its state first so
        the rebuild is a true reset. Passing a non-zero seq keeps the
        existing state and applies only the tail — useful for catch-up
        after a restart when you trust the on-disk cursor.
        """

        if since_seq == 0:
            self._objects.clear()
            self._cursor = 0

        applied = 0
        for entry in journal.replay_from(since_seq):
            if not entry.action.startswith("fabric.object."):
                # The projection should ignore non-Fabric events, but
                # `replay_from` gives us the full stream — skip efficiently.
                continue
            self.apply(entry)
            applied += 1
        return applied

    # -- Incremental apply --------------------------------------------------

    def apply(self, entry: EventEntry) -> None:
        """Fold one event into the current-state view.

        Unknown actions in the `fabric.object.*` namespace are dropped with
        a log line — defensive so a future writer can introduce a new
        action without breaking older replays that haven't been updated.
        """

        if entry.action not in ALL_FABRIC_ACTIONS:
            return

        payload: dict[str, Any] = dict(entry.payload) if isinstance(entry.payload, dict) else {}
        object_id = payload.get("object_id")
        if not object_id:
            logger.warning("Fabric projection: %s missing object_id — skipping", entry.action)
            return

        # Track cursor regardless of hit/miss so partial projections still
        # advance past events we don't care about and rebuild(since_seq=...)
        # resumes cleanly.
        seq = getattr(entry, "seq", None) or 0
        if seq > self._cursor:
            self._cursor = seq

        if entry.action == ACTION_OBJECT_CREATED:
            self._apply_created(entry, payload, object_id, seq)
        elif entry.action == ACTION_OBJECT_UPDATED:
            self._apply_updated(entry, payload, object_id, seq)
        elif entry.action == ACTION_OBJECT_ARCHIVED:
            self._apply_archived(entry, object_id, seq)

    def _apply_created(
        self,
        entry: EventEntry,
        payload: dict[str, Any],
        object_id: str,
        seq: int,
    ) -> None:
        obj = FabricObject(
            id=object_id,
            type_id=payload.get("type_id", ""),
            type_name=payload.get("type_name", ""),
            properties=dict(payload.get("properties") or {}),
            source_connector=payload.get("source_connector"),
            source_id=payload.get("source_id"),
            created_at=_as_datetime(entry.ts),
            updated_at=_as_datetime(entry.ts),
        )
        self._objects[object_id] = _ProjectedObject(
            obj=obj,
            scope=list(entry.scope),
            archived=False,
            last_seq=seq,
        )

    def _apply_updated(
        self,
        entry: EventEntry,
        payload: dict[str, Any],
        object_id: str,
        seq: int,
    ) -> None:
        existing = self._objects.get(object_id)
        if existing is None:
            # Updates for unknown objects are silently dropped — this can
            # happen if the projection was rebuilt from a truncated journal
            # and we see the update before the create it hasn't replayed.
            logger.debug("Fabric projection: update for unknown %s — dropped", object_id)
            return

        patch = dict(payload.get("properties") or {})
        merged = {**existing.obj.properties, **patch}
        existing.obj = existing.obj.model_copy(
            update={"properties": merged, "updated_at": _as_datetime(entry.ts)},
        )
        # An update event can re-scope the object — trust the event's scope
        # as the new source of truth (the writer chose to include it).
        existing.scope = list(entry.scope)
        existing.last_seq = seq

    def _apply_archived(self, entry: EventEntry, object_id: str, seq: int) -> None:
        existing = self._objects.get(object_id)
        if existing is None:
            return
        existing.archived = True
        existing.last_seq = seq

    # -- Query --------------------------------------------------------------

    def query(
        self,
        q: FabricQuery,
        *,
        requester_scopes: list[str] | None = None,
    ) -> FabricQueryResult:
        """Return the current-state view filtered by the query + the
        caller's scope. Pagination is applied AFTER scope filtering so
        ``total`` always reflects what the caller is allowed to see.

        This is the invariant that kills the pagination leak from #938:
        there is no way to compute a pre-filter total in this path —
        we don't have the un-filtered list at any point after the filter
        runs.
        """

        visible_rows = [
            row for row in self._objects.values() if not row.archived and _matches(row, q)
        ]

        public = [row.as_public() for row in visible_rows]
        filtered, _hidden = filter_visible(public, requester_scopes)

        total = len(filtered)
        offset = max(q.offset, 0)
        limit = max(q.limit, 0)
        page = filtered[offset : offset + limit] if limit else filtered[offset:]

        return FabricQueryResult(objects=page, total=total)

    # -- Diagnostics --------------------------------------------------------

    @property
    def cursor(self) -> int:
        """Latest journal seq number the projection has seen. Operators
        can persist this if they want incremental rebuild on restart.
        """

        return self._cursor

    def size(self) -> int:
        """Number of non-archived objects currently projected."""

        return sum(1 for row in self._objects.values() if not row.archived)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _matches(row: _ProjectedObject, q: FabricQuery) -> bool:
    """Apply the non-scope slice of FabricQuery (type filter, property
    filters). The linked_to path lives on the legacy SQLite store; link
    events will land in a later slice.
    """

    if q.type_id and row.obj.type_id != q.type_id:
        return False
    if q.type_name and (row.obj.type_name or "").lower() != q.type_name.lower():
        return False
    if q.filters:
        for key, want in q.filters.items():
            if row.obj.properties.get(key) != want:
                return False
    return True


def _as_datetime(ts: Any) -> datetime:
    """EventEntry.ts is always a tz-aware datetime per the journal spec,
    but stay defensive — replay over a user-supplied backend shouldn't
    crash the projection if a shim emits a string.
    """

    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return datetime.now()
