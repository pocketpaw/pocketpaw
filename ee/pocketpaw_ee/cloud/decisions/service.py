# service.py — In-process Python API + projection bootstrap for the decision graph.
# Created: 2026-05-25 (RFC 07 Slice 1) — the surface pockets, agents, and
#   jobs call. Exposes four methods on a `DecisionGraph` class:
#
#     - get(decision_id, requester_scopes=...)         → Decision | None
#     - find(actor=, since=, until=, scope_kind=,
#            pocket_id=, policy=, outcome_status=,
#            input_id=, limit=, before_ts=, before_id=,
#            requester_scopes=...)                     → list[Decision]
#     - trace(decision_id, depth=, max_fanout=,
#             requester_scopes=...)                    → TraceResult
#     - downstream(decision_id, depth=,
#                  requester_scopes=...)               → TraceResult
#
#   `explain` (NL Q&A with narrator) lives in Slice 3 and is intentionally
#   NOT on this surface yet.
# Updated: 2026-05-25 (RFC 07 Slice 2 — post-filter total) — added
#   `count(filters, requester_scopes=...) → int` so the list router can
#   return the true post-scope-filter total instead of the page size.
#   This protects the anti-probe property from RFC 07 § Privacy + audit:
#   a caller varying `limit` cannot observe a changing `total`, so
#   pre- vs post-filter counts can never be compared to infer hidden
#   rows. The new method shares the iteration path with `find()`; the
#   only divergence is that `count()` does not slice by `limit` or
#   `before_*` cursors (those reshape the page, not the filter set).
#
# Scope-filter-post-count invariant
# ---------------------------------
# Every read filters by scope BEFORE counting. The store exposes
# unfiltered iterators; this service is the one place that enforces the
# scope filter. A caller cannot probe for hidden decisions by comparing
# pre- and post-filter counts because no pre-filter count is ever
# computed here. This mirrors `FabricProjection.query`'s contract
# (`src/pocketpaw/fabric/projection.py`) that closed the #938 pagination
# leak.
#
# Pagination
# ----------
# Keyset-style on (ts DESC, id DESC) per RFC 07's perf budget. The
# `find` method takes `before_ts` + `before_id` cursors from the prior
# page; OFFSET-based pagination is avoided because it gets pathologically
# slow at scale. The reference implementation is small enough that we
# load the filtered set into memory then slice — the RFC's index design
# means the filtered set is bounded by the most-selective axis.
#
# Bootstrap
# ---------
# `init_decisions_projection()` lazily creates the singleton projection
# + store on first call. `mount_cloud()` invokes this after
# `init_realtime()` per the ee/cloud bootstrap convention. Tests get a
# fresh projection per fixture by calling `reset_projection_for_tests()`.
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from pocketpaw_ee.cloud.decisions.domain import (
    Decision,
    DecisionEdgeRecord,
    OutcomeStatus,
    ScopeKind,
)
from pocketpaw_ee.cloud.decisions.projection import DecisionProjection
from pocketpaw_ee.cloud.decisions.store import DecisionStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trace result shape — domain-side; the wire DTO lives in dto.py
# ---------------------------------------------------------------------------


@dataclass
class TraceNode:
    """One node in a depth-bounded trace. Either a Decision (with the
    full domain object hydrated) or an external input (no Decision)."""

    id: str
    kind: Literal["decision", "fabric_object", "dataref", "actor"]
    decision: Decision | None = None
    label: str = ""


@dataclass
class TraceResult:
    """Depth-bounded BFS over the decision graph. `truncated` is set
    when any node hit the fanout cap; `truncated_count` reports how many
    edges were dropped (RFC 07 amendment for gap G7)."""

    root: UUID
    nodes: dict[str, TraceNode] = field(default_factory=dict)
    edges: list[DecisionEdgeRecord] = field(default_factory=list)
    truncated: bool = False
    truncated_count: int = 0
    depth_reached: int = 0


# ---------------------------------------------------------------------------
# DecisionGraph — the Python API
# ---------------------------------------------------------------------------


class DecisionGraph:
    """In-process Python API over the materialized decision store.

    One instance per process (per org). Wire via the module-level
    singleton (`get_decision_graph()`); tests get a fresh instance by
    calling `reset_projection_for_tests()` then re-fetching.
    """

    def __init__(
        self,
        store: DecisionStore | None = None,
        projection: DecisionProjection | None = None,
    ) -> None:
        if store is None and projection is None:
            store = DecisionStore()
            projection = DecisionProjection(store=store)
        elif projection is None:
            projection = DecisionProjection(store=store)
        elif store is None:
            store = projection.store
        self._store = store
        self._projection = projection

    @property
    def projection(self) -> DecisionProjection:
        return self._projection

    @property
    def store(self) -> DecisionStore:
        return self._store

    # --- single lookup -----------------------------------------------------

    async def get(
        self,
        decision_id: UUID,
        *,
        requester_scopes: list[str] | None = None,
    ) -> Decision | None:
        """Return one Decision or None. Returns None outside the
        caller's scope (no "exists but hidden" probe)."""
        decision = self._store.get_decision(decision_id)
        if decision is None:
            return None
        if not _visible(decision, requester_scopes):
            return None
        return decision

    # --- multi-axis filter -------------------------------------------------

    async def find(
        self,
        *,
        actor: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        scope_kind: ScopeKind | None = None,
        pocket_id: str | None = None,
        policy: str | None = None,
        outcome_status: OutcomeStatus | None = None,
        input_id: str | None = None,
        limit: int = 50,
        before_ts: datetime | None = None,
        before_id: str | None = None,
        requester_scopes: list[str] | None = None,
    ) -> list[Decision]:
        """Index-driven multi-axis filter. Keyset pagination on
        (ts DESC, id DESC). The store does the row selection; this
        method applies the scope filter post-fetch and the keyset
        cursor pre-truncate.

        Scope filter is the load-bearing invariant: it runs BEFORE the
        limit slice, so a caller cannot infer hidden-row counts by
        comparing the requested limit to the returned length.
        """
        if limit < 1:
            limit = 1
        elif limit > 200:
            limit = 200

        # Stream the unfiltered candidates; apply the scope filter and
        # cursor here. The store's iter is already sorted (ts DESC, id DESC).
        results: list[Decision] = []
        for d in self._store.iter_decisions(
            actor=actor,
            pocket_id=pocket_id,
            policy=policy,
            outcome_status=outcome_status,
            scope_kind=scope_kind,
            since=since,
            until=until,
            input_id=input_id,
        ):
            if not _visible(d, requester_scopes):
                continue
            if before_ts is not None:
                # Keyset: skip rows that aren't strictly before the cursor.
                if d.ts > before_ts:
                    continue
                if d.ts == before_ts and before_id is not None:
                    if str(d.id) >= before_id:
                        continue
            results.append(d)
            if len(results) >= limit:
                break
        return results

    # --- post-scope-filter total ------------------------------------------

    async def count(
        self,
        *,
        actor: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        scope_kind: ScopeKind | None = None,
        pocket_id: str | None = None,
        policy: str | None = None,
        outcome_status: OutcomeStatus | None = None,
        input_id: str | None = None,
        requester_scopes: list[str] | None = None,
    ) -> int:
        """Count decisions matching the same filter set as ``find()``,
        post-scope-filter. ``limit`` / ``before_ts`` / ``before_id`` are
        NOT accepted — those reshape a page, not the filter set, and
        including them would make the total drift across pages and let
        a caller probe for hidden rows by comparing counts.
        """
        n = 0
        for d in self._store.iter_decisions(
            actor=actor,
            pocket_id=pocket_id,
            policy=policy,
            outcome_status=outcome_status,
            scope_kind=scope_kind,
            since=since,
            until=until,
            input_id=input_id,
        ):
            if not _visible(d, requester_scopes):
                continue
            n += 1
        return n

    # --- trace upstream ----------------------------------------------------

    async def trace(
        self,
        decision_id: UUID,
        *,
        depth: int = 3,
        max_fanout: int = 20,
        requester_scopes: list[str] | None = None,
    ) -> TraceResult:
        """Depth-bounded BFS walking `precedent` and `input` edges
        upstream. Approval + outcome edges are surfaced (the narrator
        wants them) but not walked further — they're terminal labels.

        Cycle defense: a `visited` set blocks revisits. Precedent edges
        cannot cycle (older-ts requirement) but the defense is cheap.
        """
        if depth < 1:
            depth = 1
        elif depth > 10:
            depth = 10

        root = self._store.get_decision(decision_id)
        if root is None or not _visible(root, requester_scopes):
            return TraceResult(root=decision_id)

        result = TraceResult(root=decision_id)
        result.nodes[str(decision_id)] = TraceNode(
            id=str(decision_id), kind="decision", decision=root
        )

        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(str(decision_id), 0)])

        while queue:
            cur_id, cur_depth = queue.popleft()
            if cur_id in visited:
                continue
            visited.add(cur_id)
            result.depth_reached = max(result.depth_reached, cur_depth)
            if cur_depth >= depth:
                continue

            try:
                cur_uuid = UUID(cur_id)
            except ValueError:
                continue  # external input node, not walkable

            outgoing = self._store.edges_from(cur_uuid)
            if len(outgoing) > max_fanout:
                # G7: deterministic order — precedents first (by weight
                # DESC), then inputs by insertion order, then approval
                # / outcome. We approximate by sorting precedents by
                # weight DESC and keeping insertion order otherwise.
                precedents = sorted(
                    [e for e in outgoing if e.relation == "precedent"],
                    key=lambda e: -e.weight,
                )
                others = [e for e in outgoing if e.relation != "precedent"]
                ranked = precedents + others
                kept = ranked[:max_fanout]
                dropped = ranked[max_fanout:]
                outgoing = kept
                result.truncated = True
                result.truncated_count += len(dropped)

            for edge in outgoing:
                if edge.relation in {"approval", "outcome"}:
                    # surface as terminal nodes; don't walk further
                    result.edges.append(edge)
                    if edge.target_id not in result.nodes:
                        kind: Literal["decision", "fabric_object", "dataref", "actor"] = (
                            "actor" if edge.relation == "approval" else "dataref"
                        )
                        result.nodes[edge.target_id] = TraceNode(
                            id=edge.target_id,
                            kind=kind,
                            label=edge.target_id,
                        )
                    continue

                result.edges.append(edge)

                if edge.relation == "precedent":
                    try:
                        target_uuid = UUID(edge.target_id)
                    except ValueError:
                        continue
                    target = self._store.get_decision(target_uuid)
                    if target is not None and _visible(target, requester_scopes):
                        if edge.target_id not in result.nodes:
                            result.nodes[edge.target_id] = TraceNode(
                                id=edge.target_id,
                                kind="decision",
                                decision=target,
                            )
                        queue.append((edge.target_id, cur_depth + 1))
                elif edge.relation == "input":
                    # external input — UUID-ish input ids are rare; treat
                    # most as fabric_object stubs.
                    if edge.target_id not in result.nodes:
                        result.nodes[edge.target_id] = TraceNode(
                            id=edge.target_id,
                            kind="fabric_object",
                            label=edge.target_id,
                        )
        return result

    # --- downstream --------------------------------------------------------

    async def downstream(
        self,
        decision_id: UUID,
        *,
        depth: int = 3,
        requester_scopes: list[str] | None = None,
    ) -> TraceResult:
        """Decisions that later cited this one as a precedent. Reads the
        inverse of the precedent index: `(target_id, relation='precedent')`.
        """
        if depth < 1:
            depth = 1
        elif depth > 10:
            depth = 10

        root = self._store.get_decision(decision_id)
        if root is None or not _visible(root, requester_scopes):
            return TraceResult(root=decision_id)

        result = TraceResult(root=decision_id)
        result.nodes[str(decision_id)] = TraceNode(
            id=str(decision_id), kind="decision", decision=root
        )

        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque([(str(decision_id), 0)])

        while queue:
            cur_id, cur_depth = queue.popleft()
            if cur_id in visited:
                continue
            visited.add(cur_id)
            result.depth_reached = max(result.depth_reached, cur_depth)
            if cur_depth >= depth:
                continue

            incoming = self._store.edges_to(cur_id, relation="precedent")
            for edge in incoming:
                src_str = str(edge.src_id)
                # Re-stamp the edge as "downstream" so callers can render it
                # correctly without re-deriving the inverse semantics.
                result.edges.append(
                    DecisionEdgeRecord(
                        src_id=edge.src_id,
                        target_id=cur_id,
                        relation="downstream",
                        weight=edge.weight,
                    )
                )
                if src_str not in result.nodes:
                    src_decision = self._store.get_decision(edge.src_id)
                    if src_decision is not None and _visible(src_decision, requester_scopes):
                        result.nodes[src_str] = TraceNode(
                            id=src_str,
                            kind="decision",
                            decision=src_decision,
                        )
                        queue.append((src_str, cur_depth + 1))
        return result


# ---------------------------------------------------------------------------
# Scope filter — the load-bearing invariant
# ---------------------------------------------------------------------------


def _visible(decision: Decision, requester_scopes: list[str] | None) -> bool:
    """Return True if the requester is allowed to see this decision.

    None/[] is treated as "admin / unscoped" and sees everything. Any
    non-empty list does an intersection-with-overlap on the decision's
    scope tags — the same shape FabricProjection uses
    (`pocketpaw.fabric.policy.visible`).
    """
    if not requester_scopes:
        return True
    return any(s in requester_scopes for s in decision.scope)


# ---------------------------------------------------------------------------
# Module-level singleton + bootstrap
# ---------------------------------------------------------------------------


_GRAPH: DecisionGraph | None = None


def init_decisions_projection(*, rebuild_from_journal: bool = False) -> DecisionGraph:
    """Wire the singleton DecisionGraph + projection. Idempotent.

    Called from `mount_cloud()` after `init_realtime()`. Subsequent
    calls return the existing singleton — re-mounting (tests) does not
    rebuild the store.

    Cold-start replay (RFC 09 Slice 1b)
    ----------------------------------
    When `rebuild_from_journal=True` the projection is replayed from the
    org journal starting at its persisted cursor. This folds any
    chain-forming events the journal already holds (e.g. events written
    by a producer in a prior process lifetime before the projection had
    a chance to apply them). The cursor is persisted by the store, so a
    warm restart skips the replay — only genuinely-cold starts pay the
    rebuild cost.

    `mount_cloud()` is the only caller that passes `rebuild_from_journal=
    True` today. Tests + smoke contexts get the no-replay default so a
    per-test `tmp_path` decisions.db cannot accidentally absorb events
    from a developer's real `~/.soul/journal.db`. Tests that want to
    exercise the rebuild path call `projection.rebuild(...)` directly
    against a journal fixture (see `tests/ee/test_record_decision_event
    .py::test_cold_start_rebuild_folds_pre_existing_events`).

    Replay failures are logged but do NOT block boot. The Slice 4
    reconciler (RFC 09) will catch any rows the cold start missed; the
    journal remains the source of truth.
    """
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = DecisionGraph()
        applied = 0
        if rebuild_from_journal:
            try:
                # Local import keeps the journal_dep dependency lazy —
                # tests + smoke contexts that don't mount cloud skip the
                # journal lookup entirely.
                from pocketpaw.journal_dep import get_journal

                journal = get_journal()
                cursor = _GRAPH.projection.cursor
                applied = _GRAPH.projection.rebuild(
                    journal.replay_from(cursor), since_seq=cursor
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "decisions projection cold-start rebuild skipped — "
                    "journal unavailable or replay failed; projection "
                    "will start empty and the Slice 4 reconciler will "
                    "catch up",
                    exc_info=True,
                )
        logger.info(
            "decisions projection initialized — cursor=%d count=%d "
            "events_replayed=%d",
            _GRAPH.projection.cursor,
            _GRAPH.store.count(),
            applied,
        )
    return _GRAPH


def get_decision_graph() -> DecisionGraph:
    """Return the singleton DecisionGraph. Calls `init_decisions_projection`
    if it hasn't run yet — safe in tests that import service.py without
    going through `mount_cloud`."""
    global _GRAPH
    if _GRAPH is None:
        return init_decisions_projection()
    return _GRAPH


def reset_projection_for_tests() -> None:
    """Drop the singleton so the next call to `get_decision_graph()`
    builds a fresh one. Tests use this in a fixture to get isolation
    across test cases (combined with `set_db_path(tmp_path)`)."""
    global _GRAPH
    if _GRAPH is not None:
        try:
            _GRAPH.store.close()
        except Exception:  # pragma: no cover
            logger.warning("error closing decisions store on reset", exc_info=True)
    _GRAPH = None


__all__ = [
    "DecisionGraph",
    "TraceNode",
    "TraceResult",
    "get_decision_graph",
    "init_decisions_projection",
    "reset_projection_for_tests",
]
