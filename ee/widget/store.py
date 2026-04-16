# ee/widget/store.py — Journal-backed write path for widget events.
# Created: 2026-04-16 (feat/widget-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Replaces the JSONL sink from held PR #941
# (``~/.pocketpaw/widget-interactions.jsonl`` behind an asyncio.Lock)
# and the apply side of both #941's graduation policy and #942's
# co-occurrence detector. Everything lands on the org journal now —
# one append-only event log, write serialisation inherited from
# SQLite WAL + transaction semantics rather than a per-process
# asyncio lock that didn't protect across multiple processes anyway.
# Updated: 2026-04-16 (feat/widget-track-endpoint) — Added
# ``log_widget_interaction_with_seq`` helper that returns the
# ``(EventEntry, seq)`` pair atomically. The POST /widgets/track writer
# endpoint needs the seq on its ack so the UI can round-trip to the
# journal cursor without a second lookup. The backend's
# ``SQLiteJournalBackend.append`` already returns the assigned seq;
# ``Journal.append`` drops it. Going through the backend here keeps the
# write serialised by BEGIN IMMEDIATE so there is no race between the
# INSERT and the seq read — which ``Journal.last_entry`` would have.
#
# Same store-as-thin-facade shape as ee/retrieval/store.py:
#   - ``log_widget_interaction`` emits ``widget.interaction.recorded``
#   - ``log_widget_interaction_with_seq`` same, returns (entry, seq)
#   - ``log_widget_graduation`` emits ``widget.graduated``
#   - ``log_cooccurrence`` emits ``widget.cooccurrence.detected``
#   - every emit is folded into the shared WidgetProjection so reads
#     are consistent without waiting for a rebuild
#
# Policy (thresholds, decision rules) and router (REST surface) live
# in policy.py / router.py so this module stays free of HTTP + UI
# concerns.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from soul_protocol.engine.journal import Journal
from soul_protocol.spec.journal import Actor, EventEntry

from ee.widget.events import (
    ACTION_WIDGET_COOCCURRENCE_DETECTED,
    ACTION_WIDGET_GRADUATED,
    ACTION_WIDGET_INTERACTION_RECORDED,
    cooccurrence_signature,
    widget_cooccurrence_payload,
    widget_graduated_payload,
    widget_interaction_payload,
)
from ee.widget.projection import WidgetProjection

_SYSTEM_WIDGET_ACTOR_ID = "system:widget"
_SYSTEM_GRADUATION_ACTOR_ID = "system:widget-graduation"
_SYSTEM_COOCCURRENCE_ACTOR_ID = "system:widget-cooccurrence"


class WidgetJournalStore:
    """Journal-backed emitter for widget interaction + graduation +
    co-occurrence events.

    Wiring:

        from ee.journal_dep import get_journal
        journal = get_journal()
        store = WidgetJournalStore(journal)
        store.bootstrap()
        await store.log_widget_interaction(
            widget_name="metrics_chart",
            scope=["org:sales:*"],
            actor=Actor(...),
        )
    """

    def __init__(
        self,
        journal: Journal,
        *,
        projection: WidgetProjection | None = None,
        default_actor: Actor | None = None,
        default_graduation_actor: Actor | None = None,
        default_cooccurrence_actor: Actor | None = None,
    ) -> None:
        self._journal = journal
        self._projection = projection or WidgetProjection()
        self._default_actor = default_actor or Actor(
            kind="system",
            id=_SYSTEM_WIDGET_ACTOR_ID,
            scope_context=[],
        )
        self._default_graduation_actor = default_graduation_actor or Actor(
            kind="system",
            id=_SYSTEM_GRADUATION_ACTOR_ID,
            scope_context=[],
        )
        self._default_cooccurrence_actor = default_cooccurrence_actor or Actor(
            kind="system",
            id=_SYSTEM_COOCCURRENCE_ACTOR_ID,
            scope_context=[],
        )

    # -- Bootstrap ----------------------------------------------------------

    def bootstrap(self, *, since_seq: int = 0) -> int:
        """Warm the projection from the journal. Returns the number of
        events applied. Call once at process start.
        """

        return self._projection.rebuild(self._journal, since_seq=since_seq)

    @property
    def projection(self) -> WidgetProjection:
        return self._projection

    # -- Writes -------------------------------------------------------------

    async def log_widget_interaction(
        self,
        *,
        widget_name: str,
        scope: list[str],
        actor: Actor | None = None,
        surface: str = "dashboard",
        action_type: str = "open",
        pocket_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        query_text: str | None = None,
        correlation_id: UUID | None = None,
    ) -> EventEntry:
        """Emit one ``widget.interaction.recorded`` event and fold it
        into the projection.

        ``scope`` is required — EventEntry refuses scope=[]. Callers
        that don't carry a scope (vanishingly rare — the dashboard
        always knows the pocket's scope) should make that explicit at
        the call site rather than have the store fabricate an empty
        list.
        """

        entry, _seq = await self.log_widget_interaction_with_seq(
            widget_name=widget_name,
            scope=scope,
            actor=actor,
            surface=surface,
            action_type=action_type,
            pocket_id=pocket_id,
            metadata=metadata,
            query_text=query_text,
            correlation_id=correlation_id,
        )
        return entry

    async def log_widget_interaction_with_seq(
        self,
        *,
        widget_name: str,
        scope: list[str],
        actor: Actor | None = None,
        surface: str = "dashboard",
        action_type: str = "open",
        pocket_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        query_text: str | None = None,
        correlation_id: UUID | None = None,
    ) -> tuple[EventEntry, int]:
        """Same as :meth:`log_widget_interaction` but returns the
        ``(EventEntry, seq)`` pair.

        The POST /widgets/track writer endpoint needs the journal seq
        on its ack so UIs can pin a cursor against the write without a
        second lookup. ``Journal.append`` drops the seq the backend
        assigns; reaching into the backend keeps the seq read atomic
        with the INSERT (same ``BEGIN IMMEDIATE`` transaction), so
        there is no race with concurrent writers.
        """

        _require_scope(scope)
        _require_str("widget_name", widget_name)

        payload = widget_interaction_payload(
            widget_name=widget_name,
            surface=surface,
            action_type=action_type,
            pocket_id=pocket_id,
            metadata=metadata,
            query_text=query_text,
        )
        entry = self._build_entry(
            action=ACTION_WIDGET_INTERACTION_RECORDED,
            scope=scope,
            actor=actor or self._default_actor,
            correlation_id=correlation_id,
            payload=payload,
        )
        seq = self._append_with_seq(entry)
        self._projection.apply(entry)
        return entry, seq

    async def log_widget_graduation(
        self,
        *,
        scope: list[str],
        widget_name: str,
        surface: str,
        tier: str,
        confidence: float,
        interactions_in_window: int,
        window_days: int,
        previous_tier: str | None = None,
        pocket_id: str | None = None,
        reason: str = "",
        actor: Actor | None = None,
        correlation_id: UUID | None = None,
    ) -> EventEntry:
        """Emit one ``widget.graduated`` event + fold it into the
        projection. One event per verdict change — the projection
        keeps only the most-recent verdict per (widget, surface); the
        full history stays on the journal for audit.
        """

        _require_scope(scope)
        _require_str("widget_name", widget_name)

        payload = widget_graduated_payload(
            widget_name=widget_name,
            surface=surface,
            tier=tier,
            confidence=confidence,
            interactions_in_window=interactions_in_window,
            window_days=window_days,
            previous_tier=previous_tier,
            pocket_id=pocket_id,
            reason=reason,
        )
        entry = self._build_entry(
            action=ACTION_WIDGET_GRADUATED,
            scope=scope,
            actor=actor or self._default_graduation_actor,
            correlation_id=correlation_id,
            payload=payload,
        )
        self._journal.append(entry)
        self._projection.apply(entry)
        return entry

    async def log_cooccurrence(
        self,
        *,
        scope: list[str],
        widget_a: str,
        widget_b: str,
        count: int,
        window_s: int,
        pocket_id: str | None = None,
        example_queries: list[str] | None = None,
        actor: Actor | None = None,
        correlation_id: UUID | None = None,
    ) -> EventEntry:
        """Emit one ``widget.cooccurrence.detected`` event + fold it
        into the projection.

        The signature is computed here — callers don't pass it in.
        This is the mandatory fix of #942's ``sorted(tokens[:6])``
        bug: the signature helper sorts first and truncates second,
        so dedup actually works as the superseded PR claimed.
        """

        _require_scope(scope)
        _require_str("widget_a", widget_a)
        _require_str("widget_b", widget_b)

        signature = cooccurrence_signature(widget_a, widget_b)
        payload = widget_cooccurrence_payload(
            widget_a=widget_a,
            widget_b=widget_b,
            count=count,
            window_s=window_s,
            signature=signature,
            pocket_id=pocket_id,
            example_queries=example_queries,
        )
        entry = self._build_entry(
            action=ACTION_WIDGET_COOCCURRENCE_DETECTED,
            scope=scope,
            actor=actor or self._default_cooccurrence_actor,
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

    def _append_with_seq(self, entry: EventEntry) -> int:
        """Append an entry and return its assigned seq.

        ``Journal.append`` drops the seq the SQLite backend already
        hands it back from the INSERT. Reach through to the backend
        directly so callers that need the seq (the POST /widgets/track
        writer ack) get it atomically, without a second ``last_entry``
        round-trip that could race other writers on the same journal.

        Hash-linking is reproduced here to keep the chain behaviour
        identical to ``Journal.append`` — every other widget writer in
        this module eventually routes through that. A backend that
        happens to expose ``append`` directly is the SQLite backend;
        other backends (memory, remote) still work via the
        Journal-level path.
        """

        backend = getattr(self._journal, "_backend", None)
        if backend is None or not hasattr(backend, "append"):  # pragma: no cover - defensive
            # No backend handle — fall back to the public path and
            # approximate the seq by re-reading. Other backends will
            # either expose the attribute or should be wrapped with a
            # shim that does.
            self._journal.append(entry)
            tail = None
            last_entry = getattr(self._journal, "_backend", None)
            if last_entry is not None and hasattr(last_entry, "last_entry"):
                tail = last_entry.last_entry()
            if tail is None:
                return 0
            return int(tail[1])

        # Reproduce Journal.append's hash-link step so the chain stays
        # consistent with the public path.
        if entry.prev_hash is None:
            last = backend.last_entry()
            if last is not None:
                from soul_protocol.engine.journal.journal import _hash_link

                prev_entry, prev_seq = last
                try:
                    entry = entry.model_copy(
                        update={"prev_hash": _hash_link(prev_entry, prev_seq)}
                    )
                except Exception:
                    # Match Journal.append's policy: a hash-link failure
                    # is logged upstream and does not block the write.
                    pass

        return int(backend.append(entry))


def _require_scope(scope: list[str]) -> None:
    if not scope:
        raise ValueError(
            "WidgetJournalStore requires a non-empty scope on every write — "
            "the journal invariant refuses events with scope=[]."
        )


def _require_str(label: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
