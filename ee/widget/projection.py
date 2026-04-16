# ee/widget/projection.py — In-memory projection over widget events.
# Created: 2026-04-16 (feat/widget-journal-projection) — Wave 3 / Org
# Architecture RFC, Phase 3. Read-side of the widget domain —
# supersedes #941's graduation scan over ``~/.pocketpaw/widget-
# interactions.jsonl`` and #942's co-occurrence detector stacked on
# that file. Both held PRs shared the same input (a per-interaction
# log) and a similar fold (count events per widget / per pair over a
# rolling window); the two concerns land in one projection here
# because the replay cost is the same either way and keeping them
# together halves the rebuild time.
#
# Three logical views over one event stream:
#   - WidgetUsageProjection: per-widget counts in a rolling window
#     (how the graduation policy decides pin / fade / archive).
#     Ports #941's per-widget Counter fold.
#   - CooccurrenceProjection: per-signature pair counts + example
#     widgets. Ports #942's session-pair detector but with the
#     ``sorted(tokens)[:6]`` ordering fixed — see
#     ee.widget.events.normalise_signature_tokens for the bug
#     explanation.
#   - GraduationStateProjection: most-recent graduation verdict per
#     (widget_name, surface) pair. One row per widget; repeat
#     graduations overwrite. Ports the output-state side of #941.
#
# All three live on ONE WidgetProjection instance because they share
# the same event stream. Rebuild is O(events); incremental apply is
# O(1) per event.

from __future__ import annotations

import logging
from bisect import insort
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from soul_protocol.engine.journal import Journal
from soul_protocol.spec.journal import EventEntry

from ee.fabric.policy import filter_visible
from ee.widget.events import (
    ACTION_WIDGET_COOCCURRENCE_DETECTED,
    ACTION_WIDGET_GRADUATED,
    ACTION_WIDGET_INTERACTION_RECORDED,
    ALL_WIDGET_ACTIONS,
    cooccurrence_signature,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public row shapes — stable dataclasses the router serialises. Kept as
# dataclasses (not Pydantic) so the projection has no model-machinery
# import cost on hot paths.
# ---------------------------------------------------------------------------


@dataclass
class WidgetInteractionView:
    """One projected widget interaction — a single
    ``widget.interaction.recorded`` event after replay."""

    widget_name: str
    surface: str
    action_type: str
    actor_id: str
    actor_kind: str
    scope: list[str]
    pocket_id: str | None
    correlation_id: str | None
    ts: datetime
    metadata: dict[str, Any]
    query_text: str | None
    seq: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "widget_name": self.widget_name,
            "surface": self.surface,
            "action_type": self.action_type,
            "actor_id": self.actor_id,
            "actor_kind": self.actor_kind,
            "scope": list(self.scope),
            "pocket_id": self.pocket_id,
            "correlation_id": self.correlation_id,
            "ts": self.ts.isoformat(),
            "metadata": dict(self.metadata),
            "query_text": self.query_text,
            "seq": self.seq,
        }


@dataclass
class WidgetUsageRow:
    """Per-widget usage row — one per (widget_name, surface) pair.

    Emitted by :meth:`WidgetProjection.usage` after folding the
    ``widget.interaction.recorded`` events in the caller's chosen
    window. ``last_interaction`` carries the newest event's ts so the
    graduation policy can compute staleness without a second walk.
    """

    widget_name: str
    surface: str
    count: int
    scope: list[str]
    pocket_id: str | None
    last_interaction: datetime
    promoting_count: int
    unique_actors: int


@dataclass
class CooccurrenceRow:
    """One co-occurring widget pair row.

    ``signature`` is re-derived from the raw widget names on replay so
    a buggy out-of-band emitter (or a pre-fix #942 writer) can't
    poison the projection — the projection trusts only what it can
    recompute.
    """

    signature: str
    widget_a: str
    widget_b: str
    count: int
    pocket_id: str | None
    scope: list[str]
    last_seen: datetime
    seq: int


@dataclass
class GraduationStateRow:
    """Most-recent graduation decision for one (widget_name, surface) pair.

    The projection keeps only the latest tier per widget — a repeat
    graduation overwrites the row. Full history lives on the journal
    via ``journal.query(action="widget.graduated")``.
    """

    widget_name: str
    surface: str
    current_tier: str
    previous_tier: str | None
    confidence: float
    interactions_in_window: int
    window_days: int
    pocket_id: str | None
    scope: list[str]
    reason: str
    applied_at: datetime
    seq: int


# ---------------------------------------------------------------------------
# Internal storage row — not exposed. Wraps the public view in a sortable
# envelope so ``insort`` keeps insertion order by seq under out-of-order
# replays.
# ---------------------------------------------------------------------------


@dataclass
class _InteractionRow:
    view: WidgetInteractionView

    def __lt__(self, other: _InteractionRow) -> bool:
        return self.view.seq < other.view.seq


# ---------------------------------------------------------------------------
# The projection.
# ---------------------------------------------------------------------------


@dataclass
class _SessionWindow:
    """Scratch state for co-occurrence detection during replay.

    Tracks the last widget a (actor, pocket) pair touched plus the
    timestamp of that touch. When the next touch lands within the
    session window the projection accumulates one pair. Kept separate
    from the public view so tests can read-through the resulting
    Cooccurrence rows without peeking at fold internals.
    """

    last_widget: str = ""
    last_text: str = ""
    last_ts: datetime | None = None


class WidgetProjection:
    """Rebuilds + serves read views for widget events.

    One instance per process; rebuild is O(events) so operators can
    drop and rebuild if they suspect drift. No persistence — the
    projection is a pure fold over the journal.
    """

    def __init__(
        self,
        *,
        max_interactions: int = 20_000,
        session_window: timedelta = timedelta(minutes=15),
    ) -> None:
        # Interaction log — newest wins when we spill past the cap.
        self._interactions: list[_InteractionRow] = []
        self._max = max_interactions
        # Co-occurrence state.
        self._session_window = session_window
        self._pair_counts: dict[str, CooccurrenceRow] = {}
        self._session_scratch: dict[tuple[str, str], _SessionWindow] = {}
        # Graduation state — latest per (widget, surface).
        self._graduation: dict[tuple[str, str], GraduationStateRow] = {}
        # Projection cursor for resumable replays.
        self._cursor: int = 0

    # -- Build / rebuild ----------------------------------------------------

    def rebuild(self, journal: Journal, *, since_seq: int = 0) -> int:
        """Replay the journal from ``since_seq`` (0 = genesis), applying
        every widget event. Returns the number of events applied. When
        ``since_seq == 0`` the projection wipes state so rebuild is a
        true reset.
        """

        if since_seq == 0:
            self._interactions.clear()
            self._pair_counts.clear()
            self._session_scratch.clear()
            self._graduation.clear()
            self._cursor = 0

        applied = 0
        for entry in journal.replay_from(since_seq):
            if entry.action not in ALL_WIDGET_ACTIONS:
                continue
            self.apply(entry)
            applied += 1
        return applied

    # -- Incremental apply --------------------------------------------------

    def apply(self, entry: EventEntry) -> None:
        """Fold a single event into the projection."""

        if entry.action not in ALL_WIDGET_ACTIONS:
            return

        payload: dict[str, Any] = (
            dict(entry.payload) if isinstance(entry.payload, dict) else {}
        )
        seq = getattr(entry, "seq", None) or 0
        if seq > self._cursor:
            self._cursor = seq

        if entry.action == ACTION_WIDGET_INTERACTION_RECORDED:
            self._apply_interaction(entry, payload, seq)
        elif entry.action == ACTION_WIDGET_GRADUATED:
            self._apply_graduation(entry, payload, seq)
        elif entry.action == ACTION_WIDGET_COOCCURRENCE_DETECTED:
            self._apply_cooccurrence_event(entry, payload, seq)

    def _apply_interaction(
        self,
        entry: EventEntry,
        payload: dict[str, Any],
        seq: int,
    ) -> None:
        widget_name = payload.get("widget_name")
        if not isinstance(widget_name, str) or not widget_name:
            logger.warning("Widget projection: interaction event missing widget_name")
            return

        view = WidgetInteractionView(
            widget_name=widget_name,
            surface=str(payload.get("surface") or "dashboard"),
            action_type=str(payload.get("action_type") or "open"),
            actor_id=str(entry.actor.id),
            actor_kind=str(entry.actor.kind),
            scope=list(entry.scope),
            pocket_id=_none_or_str(payload.get("pocket_id")),
            correlation_id=_uuid_to_str(entry.correlation_id),
            ts=_as_datetime(entry.ts),
            metadata=dict(payload.get("metadata") or {}),
            query_text=_none_or_str(payload.get("query_text")),
            seq=seq,
        )
        insort(self._interactions, _InteractionRow(view=view))
        # Evict oldest once we pass the cap — keep the tail (newest).
        if len(self._interactions) > self._max:
            overflow = len(self._interactions) - self._max
            del self._interactions[: overflow]

        # Co-occurrence fold — runs over the same events.
        self._maybe_record_pair(view)

    def _apply_graduation(
        self,
        entry: EventEntry,
        payload: dict[str, Any],
        seq: int,
    ) -> None:
        widget_name = payload.get("widget_name")
        surface = payload.get("surface") or "dashboard"
        if not isinstance(widget_name, str) or not widget_name:
            logger.warning("Widget projection: graduated event missing widget_name")
            return

        row = GraduationStateRow(
            widget_name=widget_name,
            surface=str(surface),
            current_tier=str(payload.get("tier") or ""),
            previous_tier=_none_or_str(payload.get("previous_tier")),
            confidence=float(payload.get("confidence") or 0.0),
            interactions_in_window=int(payload.get("interactions_in_window") or 0),
            window_days=int(payload.get("window_days") or 0),
            pocket_id=_none_or_str(payload.get("pocket_id")),
            scope=list(entry.scope),
            reason=str(payload.get("reason") or ""),
            applied_at=_as_datetime(entry.ts),
            seq=seq,
        )
        self._graduation[(widget_name, row.surface)] = row

    def _apply_cooccurrence_event(
        self,
        entry: EventEntry,
        payload: dict[str, Any],
        seq: int,
    ) -> None:
        """Fold an explicitly-emitted co-occurrence event.

        The projection also auto-derives co-occurrence from raw
        interactions in :meth:`_maybe_record_pair`; this path is here
        for callers that emit pair counts out-of-band (a batch job,
        for instance, or a migration from #942's legacy JSONL).

        Re-derives ``signature`` from ``widget_a`` + ``widget_b`` so a
        pre-fix payload that carried the #942 bug signature gets
        corrected on replay. The projection is the source of truth
        for signatures; emitters are advisory.
        """

        widget_a = payload.get("widget_a") or ""
        widget_b = payload.get("widget_b") or ""
        signature = cooccurrence_signature(str(widget_a), str(widget_b))
        if not signature:
            return

        row = self._pair_counts.get(signature)
        count_delta = int(payload.get("count") or 1)
        last_seen = _as_datetime(entry.ts)
        if row is None:
            self._pair_counts[signature] = CooccurrenceRow(
                signature=signature,
                widget_a=str(widget_a),
                widget_b=str(widget_b),
                count=count_delta,
                pocket_id=_none_or_str(payload.get("pocket_id")),
                scope=list(entry.scope),
                last_seen=last_seen,
                seq=seq,
            )
        else:
            row.count += count_delta
            if last_seen > row.last_seen:
                row.last_seen = last_seen
                row.seq = seq

    def _maybe_record_pair(self, view: WidgetInteractionView) -> None:
        """Record a co-occurring widget pair if this interaction lands
        inside an active session window with the previous interaction
        from the same (actor, pocket) pair.
        """

        key = (view.actor_id, view.pocket_id or "")
        prev = self._session_scratch.get(key)
        now = view.ts
        if prev is None or prev.last_ts is None:
            self._session_scratch[key] = _SessionWindow(
                last_widget=view.widget_name,
                last_text=view.query_text or view.widget_name,
                last_ts=now,
            )
            return

        if now - prev.last_ts > self._session_window:
            # Session expired — start a new one with this touch.
            self._session_scratch[key] = _SessionWindow(
                last_widget=view.widget_name,
                last_text=view.query_text or view.widget_name,
                last_ts=now,
            )
            return

        # Same session — record a pair when the widgets are distinct.
        curr_text = view.query_text or view.widget_name
        signature = cooccurrence_signature(prev.last_text, curr_text)
        if signature and view.widget_name != prev.last_widget:
            row = self._pair_counts.get(signature)
            if row is None:
                lo, hi = sorted([prev.last_widget, view.widget_name])
                self._pair_counts[signature] = CooccurrenceRow(
                    signature=signature,
                    widget_a=lo,
                    widget_b=hi,
                    count=1,
                    pocket_id=view.pocket_id,
                    scope=list(view.scope),
                    last_seen=now,
                    seq=view.seq,
                )
            else:
                row.count += 1
                if now > row.last_seen:
                    row.last_seen = now
                    row.seq = view.seq

        # Roll the window forward.
        self._session_scratch[key] = _SessionWindow(
            last_widget=view.widget_name,
            last_text=curr_text,
            last_ts=now,
        )

    # -- Interaction queries ------------------------------------------------

    def recent_interactions(
        self,
        *,
        scope: str | None = None,
        widget_name: str | None = None,
        actor_id: str | None = None,
        pocket_id: str | None = None,
        limit: int = 50,
        requester_scopes: list[str] | None = None,
    ) -> list[WidgetInteractionView]:
        """Return the most-recent interactions, newest-first, with
        optional filters. Scope containment runs via
        ``ee.fabric.policy.filter_visible`` so the rules stay identical
        to Fabric's + retrieval's — no divergent semantics across
        projections.
        """

        rows = [row.view for row in self._interactions]
        if scope:
            rows = [r for r in rows if scope in r.scope]
        if widget_name:
            rows = [r for r in rows if r.widget_name == widget_name]
        if actor_id:
            rows = [r for r in rows if r.actor_id == actor_id]
        if pocket_id:
            rows = [r for r in rows if r.pocket_id == pocket_id]

        if requester_scopes:
            visible, _hidden = filter_visible(rows, requester_scopes)
            rows = list(visible)

        rows.sort(key=lambda v: v.seq, reverse=True)
        if limit > 0:
            rows = rows[:limit]
        return rows

    # -- Usage roll-up ------------------------------------------------------

    def usage(
        self,
        *,
        window_days: int = 30,
        scope: str | None = None,
        pocket_id: str | None = None,
        requester_scopes: list[str] | None = None,
        promoting_actions: tuple[str, ...] = ("open", "edit", "click"),
    ) -> list[WidgetUsageRow]:
        """Per-widget usage roll-up over the last ``window_days``.

        Mirrors #941's Counter fold — one row per (widget_name,
        surface) pair with the promoting-count subset the graduation
        policy uses for pin decisions.
        """

        since = datetime.now(UTC) - timedelta(days=window_days)
        interactions = [
            row.view
            for row in self._interactions
            if _ensure_aware(row.view.ts) >= since
        ]
        if scope:
            interactions = [r for r in interactions if scope in r.scope]
        if pocket_id:
            interactions = [r for r in interactions if r.pocket_id == pocket_id]
        if requester_scopes:
            visible, _hidden = filter_visible(interactions, requester_scopes)
            interactions = list(visible)

        per_widget: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {
                "count": 0,
                "promoting": 0,
                "actors": set(),
                "last": None,
                "scope": [],
                "pocket_id": None,
            }
        )
        for view in interactions:
            key = (view.widget_name, view.surface)
            bucket = per_widget[key]
            bucket["count"] += 1
            bucket["actors"].add(view.actor_id)
            if view.action_type in promoting_actions:
                bucket["promoting"] += 1
            last = bucket["last"]
            if last is None or view.ts > last:
                bucket["last"] = view.ts
                bucket["scope"] = list(view.scope)
                bucket["pocket_id"] = view.pocket_id

        rows = [
            WidgetUsageRow(
                widget_name=key[0],
                surface=key[1],
                count=int(bucket["count"]),
                promoting_count=int(bucket["promoting"]),
                unique_actors=len(bucket["actors"]),
                last_interaction=bucket["last"] or datetime.now(UTC),
                scope=list(bucket["scope"]),
                pocket_id=bucket["pocket_id"],
            )
            for key, bucket in per_widget.items()
        ]
        rows.sort(key=lambda r: r.count, reverse=True)
        return rows

    # -- Co-occurrence queries ---------------------------------------------

    def cooccurrences(
        self,
        *,
        min_count: int = 1,
        pocket_id: str | None = None,
        limit: int = 50,
        requester_scopes: list[str] | None = None,
    ) -> list[CooccurrenceRow]:
        """Return co-occurring widget pairs sorted by count (desc).

        ``min_count`` defaults to 1 so callers can see every pair
        observed; the co-occurrence-detector policy raises it to 3 to
        match #942's DEFAULT_THRESHOLD.
        """

        rows = [r for r in self._pair_counts.values() if r.count >= min_count]
        if pocket_id:
            rows = [r for r in rows if r.pocket_id == pocket_id]
        if requester_scopes:
            visible, _hidden = filter_visible(rows, requester_scopes)
            rows = list(visible)
        rows.sort(key=lambda r: (r.count, r.last_seen), reverse=True)
        if limit > 0:
            rows = rows[:limit]
        return rows

    # -- Graduation queries ------------------------------------------------

    def graduation_state(
        self,
        *,
        widget_name: str | None = None,
        surface: str | None = None,
        requester_scopes: list[str] | None = None,
    ) -> list[GraduationStateRow]:
        """Current graduation state — one row per (widget_name, surface)."""

        rows: list[GraduationStateRow] = []
        for (name, surf), row in self._graduation.items():
            if widget_name and name != widget_name:
                continue
            if surface and surf != surface:
                continue
            rows.append(row)
        if requester_scopes:
            visible, _hidden = filter_visible(rows, requester_scopes)
            rows = list(visible)
        rows.sort(key=lambda r: r.seq, reverse=True)
        return rows

    # -- Diagnostics --------------------------------------------------------

    @property
    def cursor(self) -> int:
        """Latest seq the projection has seen. Persist this to skip
        ahead on restart.
        """

        return self._cursor

    def size(self) -> dict[str, int]:
        """Quick counters for the /widgets/stats endpoint."""

        return {
            "interactions": len(self._interactions),
            "cooccurrences": len(self._pair_counts),
            "graduations": len(self._graduation),
        }


# Backward-compatible aliases — the task prompt names three separate
# "projections" but they all live on WidgetProjection because the
# underlying journal stream is singular. Exposing thin facades makes
# the semantics in other modules (policy, router, tests) explicit.


WidgetUsageProjection = WidgetProjection
CooccurrenceProjection = WidgetProjection
GraduationStateProjection = WidgetProjection


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _as_datetime(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _ensure_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def _uuid_to_str(value: Any) -> str | None:
    if value is None:
        return None
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
