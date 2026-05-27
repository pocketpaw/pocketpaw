# projection.py — Fold journal events into Decisions + edge rows.
# Created: 2026-05-25 (RFC 07 Slice 1) — the read path that turns the
#   append-only journal into queryable Decision objects. Ported from the
#   /tmp/team-rfc07 prototype (8 green tests there) but rewritten for:
#     - SQLite store (DecisionStore) instead of in-memory dicts
#     - A1 — `approvers: list[ApproverRef]` carrying approved_at + position
#     - A2 — ONLY `decision.graduated` closes a chain. Fabric writes
#       (`fabric.object.*`) CONTRIBUTE inputs but do NOT close. This
#       decouples the projection from Fabric's namespace.
#     - A3 — Precedent supply order: (1) agent payload first, (2)
#       projection fallback same-pocket / same-action / nearest-ts.
#       Out-of-band reconciler deferred.
#     - G6 — hash chain includes prev_hash (see domain.compute_hash_link).
#
#   Contract mirrors FabricProjection (src/pocketpaw/fabric/projection.py):
#     - rebuild(journal, since_seq=0): full or incremental replay
#     - apply(entry): incremental single-event update
#     - cursor property for restart catch-up
#   The instance is process-local; SQLite handles cross-process via WAL
#   but a single writer is the only safe pattern (one projection per org).
#
#   Note on `decision.outcome_attached`: that event action is being added
#   to soul-protocol's ACTION_NAMESPACES in a parallel PR (Slice 0). The
#   projection consumes it as a STRING LITERAL here — when Slice 0
#   merges, no code change is needed; the namespace registration is
#   advisory, not enforced.
#
#   2026-05-25 (RFC 07 Slice 3a) — added a tiny post-apply hook registry
#   so the explain cache layer (`decisions.explain.cache`) can invalidate
#   cached entries when a new Decision lands. The registry is additive:
#   hooks default to empty, every existing apply() path now funnels its
#   return through `_emit(decision)` which fires hooks before returning.
#   This keeps layering one-way (explain → decisions) — the projection
#   never imports the cache.
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from soul_protocol.spec.journal import Actor, EventEntry

from pocketpaw_ee.cloud.decisions.domain import (
    ApproverRef,
    Decision,
    DecisionEdgeRecord,
    DecisionRef,
    InputRef,
    OutcomeRef,
    compute_hash_link,
)
from pocketpaw_ee.cloud.decisions.store import DecisionStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action namespace constants
# ---------------------------------------------------------------------------
# A2 (gap G2): ONLY `decision.graduated` closes a chain. Fabric writes
# CONTRIBUTE inputs but do NOT close. Rejection chains end on
# `decision.graduated` too — the payload carries `passed=false`.
_TERMINAL_ACTIONS = frozenset({"decision.graduated"})

# Fabric write actions — they don't close a chain (A2) but they do
# contribute the target object as an InputRef so the decision row carries
# "this is the object the write hit."
_FABRIC_WRITE_ACTIONS = frozenset(
    {
        "fabric.object.created",
        "fabric.object.updated",
        "fabric.object.archived",
    }
)

# The full set the projection cares about; everything else is dropped.
_TRACKED_ACTIONS = (
    frozenset(
        {
            "agent.proposed",
            "human.corrected",
            "policy.evaluated",
            # String literal — namespace registration lives in Slice 0.
            "decision.outcome_attached",
        }
    )
    | _TERMINAL_ACTIONS
    | _FABRIC_WRITE_ACTIONS
)


# ---------------------------------------------------------------------------
# Pending-chain bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _PendingChain:
    """In-flight decision chain — accumulates events under one
    correlation_id until a `decision.graduated` event closes it."""

    correlation_id: UUID
    intent: str = ""
    action: str = ""
    decided_by: Actor | None = None
    scope: list[str] = field(default_factory=list)
    pocket_id: str | None = None
    inputs: list[InputRef] = field(default_factory=list)
    approvers: list[ApproverRef] = field(default_factory=list)
    instinct_policy: str | None = None
    instinct_policy_passed: bool | None = None
    precedent_hints: list[DecisionRef] = field(default_factory=list)
    last_seq: int = 0
    proposed_at: datetime | None = None
    payload_acc: dict[str, Any] = field(default_factory=dict)
    # A2: if the chain saw a `fabric.object.*` write the projection
    # records the target object as an additional input — but the chain
    # stays open until `decision.graduated` lands.
    fabric_write_payload: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


class DecisionProjection:
    """Replay journal events and materialize Decision rows + edge rows.

    Wire contract matches FabricProjection:
      - one instance per process (per org)
      - `rebuild(journal, since_seq=0)` for full / incremental replay
      - `apply(entry)` for inline per-event update inside the journal
        write path
      - `cursor` property surfaces the latest seen seq for restart catch-up
    """

    def __init__(self, store: DecisionStore | None = None) -> None:
        self._store = store if store is not None else DecisionStore()
        self._pending: dict[UUID, _PendingChain] = {}
        self._last_hash_per_correlation: dict[UUID, str] = {}
        self._cursor: int = self._store.get_cursor()
        # Slice 3a — post-apply hook registry. The explain cache
        # registers an invalidator here; future consumers (search index,
        # notification fan-out, etc.) can pile on without touching the
        # projection's hot path.
        self._post_apply_hooks: list[Callable[[Decision], None]] = []

    @property
    def store(self) -> DecisionStore:
        return self._store

    @property
    def cursor(self) -> int:
        return self._cursor

    def register_post_apply_hook(self, hook: Callable[[Decision], None]) -> None:
        """Register a callback fired after every successful Decision emit.

        Hooks are best-effort — exceptions raised by a hook are caught
        and logged so one bad subscriber can't block the projection's
        write path. Hooks see the freshly-emitted Decision (the same
        object the apply() return value would carry); they MUST NOT
        mutate it.

        Idempotence — the same callable can be registered more than once;
        each registration adds another invocation. Callers that need
        once-only semantics should track their own registration state.
        """
        self._post_apply_hooks.append(hook)

    def _emit(self, decision: Decision | None) -> Decision | None:
        """Fire post-apply hooks for a freshly-emitted Decision, then
        return it. The single chokepoint for apply()'s return values so
        every emit path triggers the hooks symmetrically."""
        if decision is None:
            return None
        for hook in self._post_apply_hooks:
            try:
                hook(decision)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "post-apply hook %s raised — continuing",
                    getattr(hook, "__qualname__", repr(hook)),
                    exc_info=True,
                )
        return decision

    # --- rebuild / apply ----------------------------------------------------

    def rebuild(self, journal_iter: Iterable[EventEntry], *, since_seq: int = 0) -> int:
        """Replay events. Returns count applied. If `since_seq == 0`, the
        store is reset first.

        Idempotence: applying the same entry twice produces the same
        Decision rows (the chain's `correlation_id` is the dedup key
        within the projection's pending dict; once the chain emits, the
        store's `INSERT OR REPLACE` handles re-emission). A rebuild from
        seq=0 yields the same final state as an incremental replay from
        any intermediate cursor — pinned by `test_rebuild_idempotent`.
        """
        if since_seq == 0:
            self._store.reset()
            self._pending = {}
            self._last_hash_per_correlation = {}
            self._cursor = 0
            # Re-hydrate the per-correlation last hash from the store on
            # incremental replay so the chain links across restarts.
        else:
            self._rehydrate_correlation_state()

        applied = 0
        for entry in journal_iter:
            entry_seq = getattr(entry, "seq", None)
            # Skip events whose seq is at or below the cursor — but if
            # the entry has no seq at all (older soul-protocol wheels
            # don't expose it yet), apply unconditionally. The cursor
            # itself only advances when seq is present.
            if entry_seq is not None and entry_seq <= since_seq:
                continue
            self.apply(entry)
            applied += 1
        return applied

    def _rehydrate_correlation_state(self) -> None:
        """Refill the per-correlation `last_hash` map from the store after
        an incremental restart. Without this, hash chain `prev_hash` would
        be empty for the first new event after a restart, breaking the
        chain. We only need the LAST hash per correlation."""
        # Cheap scan — group by correlation_id, pick the latest by ts.
        # The schema has an index on correlation_id; the count is bounded
        # by the number of open + recently-closed chains.
        for decision in self._store.iter_decisions():
            if decision.correlation_id is None:
                continue
            self._last_hash_per_correlation[decision.correlation_id] = decision.hash_link

    def apply(self, entry: EventEntry) -> Decision | None:
        """Fold one event. Returns a Decision when a chain closes
        (`decision.graduated`), or when a `decision.outcome_attached`
        mutates an already-emitted Decision, or for a degenerate
        single-write Decision (no correlation_id). Returns None while a
        chain is still accumulating.
        """
        if entry.action not in _TRACKED_ACTIONS:
            return None

        seq = getattr(entry, "seq", None) or 0
        if seq > self._cursor:
            self._cursor = seq
            # Persist so a restart skips this event without re-applying.
            try:
                self._store.set_cursor(seq)
            except Exception:  # pragma: no cover — store IO errors logged but non-fatal here
                logger.warning("failed to persist decision-projection cursor", exc_info=True)

        # Special-case the late outcome attach — doesn't accumulate into a
        # pending chain; mutates an already-emitted Decision in place.
        if entry.action == "decision.outcome_attached":
            return self._emit(self._apply_outcome_attached(entry))

        correlation_id = entry.correlation_id
        if correlation_id is None:
            # RFC line 274 — degenerate Decision: a one-off Fabric write
            # with no correlation. The graph still represents it, the
            # explain narrator still has something to say.
            if entry.action in _FABRIC_WRITE_ACTIONS:
                return self._emit(self._emit_degenerate(entry))
            # Other actions without a correlation aren't actionable.
            logger.debug(
                "decision projection: %s without correlation_id — dropped",
                entry.action,
            )
            return None

        chain = self._pending.get(correlation_id)
        if chain is None:
            chain = _PendingChain(correlation_id=correlation_id)
            self._pending[correlation_id] = chain
        chain.last_seq = max(chain.last_seq, seq)

        if entry.action == "agent.proposed":
            self._fold_proposed(chain, entry)
        elif entry.action == "human.corrected":
            self._fold_corrected(chain, entry)
        elif entry.action == "policy.evaluated":
            self._fold_policy(chain, entry)
        elif entry.action in _FABRIC_WRITE_ACTIONS:
            # A2: contribute, do not close.
            self._fold_fabric_write(chain, entry)
        elif entry.action in _TERMINAL_ACTIONS:
            return self._emit(self._close_chain(chain, entry))
        return None

    # --- fold helpers -------------------------------------------------------

    def _fold_proposed(self, chain: _PendingChain, entry: EventEntry) -> None:
        payload = entry.payload or {}
        chain.intent = payload.get("intent", chain.intent) or chain.intent
        chain.action = payload.get("action", chain.action) or chain.action or "propose"
        chain.decided_by = entry.actor
        chain.scope = list(entry.scope)
        chain.pocket_id = payload.get("pocket_id", chain.pocket_id)
        chain.proposed_at = entry.ts

        # inputs in payload: list of {kind, id, label?, point_in_time?} or
        # bare-string ids (assumed fabric_object).
        for raw in payload.get("inputs", []) or []:
            if isinstance(raw, dict):
                try:
                    chain.inputs.append(
                        InputRef(
                            kind=raw.get("kind", "fabric_object"),
                            id=raw["id"],
                            label=raw.get("label", ""),
                            point_in_time=_parse_dt(raw.get("point_in_time")),
                        )
                    )
                except (KeyError, ValueError):
                    continue
            elif isinstance(raw, str):
                chain.inputs.append(InputRef(kind="fabric_object", id=raw))

        # A3 path 1: agent supplies precedents in the proposed payload.
        for raw in payload.get("precedents", []) or []:
            if not isinstance(raw, dict):
                continue
            try:
                chain.precedent_hints.append(
                    DecisionRef(
                        decision_id=UUID(raw["decision_id"]),
                        relation="precedent",
                        weight=float(raw.get("weight", 1.0)),
                    )
                )
            except (KeyError, ValueError):
                continue

        # Payload bag — keep the agent's free-form data for the narrator.
        data = payload.get("data")
        if isinstance(data, dict):
            chain.payload_acc.update(data)

    def _fold_corrected(self, chain: _PendingChain, entry: EventEntry) -> None:
        chain.approvers.append(
            ApproverRef(
                actor=entry.actor,
                approved_at=entry.ts,
                position=len(chain.approvers),
            )
        )
        note = (entry.payload or {}).get("note")
        if note:
            chain.payload_acc.setdefault("approver_notes", []).append(
                {"actor": entry.actor.id, "note": note}
            )

    def _fold_policy(self, chain: _PendingChain, entry: EventEntry) -> None:
        """Each `policy.evaluated` event overrides the chain's current
        policy state. The *last* policy seen before terminal close is
        what counts (RFC line 264). A rejection only sticks if the last
        observed state is `passed=False`. We stash the rejection reason
        whenever a fail event lands so the narrator has it even if a
        later pass overrides — the payload remembers "we considered
        this, here's why instinct first balked." `has_rejection` is
        re-derived from `instinct_policy_passed` at close time, so a
        false→true sequence (instinct asked for human → human approved →
        instinct re-checked → passed) ends with `has_rejection=False`.
        """
        payload = entry.payload or {}
        policy_name = payload.get("policy")
        if policy_name:
            chain.instinct_policy = policy_name
            passed = bool(payload.get("passed", False))
            chain.instinct_policy_passed = passed
            if not passed:
                reason = payload.get("reason")
                if reason:
                    chain.payload_acc["rejection_reason"] = reason

    def _fold_fabric_write(self, chain: _PendingChain, entry: EventEntry) -> None:
        """A2: a fabric write contributes the target object as an input
        AND records the canonical write payload, but does NOT close the
        chain. The chain stays open until `decision.graduated` lands."""
        payload = entry.payload or {}
        chain.fabric_write_payload = dict(payload)
        target = payload.get("object_id")
        if target and not any(i.id == target for i in chain.inputs):
            chain.inputs.append(InputRef(kind="fabric_object", id=target, label=str(target)))

    # --- close --------------------------------------------------------------

    def _close_chain(self, chain: _PendingChain, terminal: EventEntry) -> Decision | None:
        """Emit a Decision row + its edge rows in a single transaction."""
        if chain.decided_by is None:
            # Chain closed without ever seeing `agent.proposed` — nothing
            # to emit. This can happen on a malformed event stream; log
            # and drop so the projection stays available.
            logger.warning(
                "decision projection: chain %s closed without proposed event — skipping",
                chain.correlation_id,
            )
            self._pending.pop(chain.correlation_id, None)
            return None

        decision_id = uuid4()
        scope_kind = _infer_scope_kind(chain.scope, chain.pocket_id)

        # Derive rejection from the freshest available signal:
        #   1. terminal `decision.graduated` payload's `passed` flag (if set)
        #   2. fall back to the LAST observed `instinct_policy_passed`
        # A chain that went policy(fail) → human → policy(pass) → graduated
        # ends with `instinct_policy_passed=True` and is NOT rejected.
        terminal_payload = terminal.payload or {}
        terminal_passed = terminal_payload.get("passed")
        if terminal_passed is False:
            rejected = True
        elif terminal_passed is True:
            rejected = False
        else:
            rejected = chain.instinct_policy_passed is False

        outcome: OutcomeRef | None = None
        if rejected:
            outcome = OutcomeRef(
                outcome_id=uuid4(),
                status="rejected",
                landed_at=terminal.ts,
                metered=False,
            )

        decision = Decision(
            id=decision_id,
            ts=chain.proposed_at or terminal.ts,
            decided_by=chain.decided_by,
            scope=chain.scope,
            scope_kind=scope_kind,
            intent=chain.intent or "(no intent recorded)",
            action=chain.action or "graduated",
            inputs=chain.inputs,
            approvers=chain.approvers,
            instinct_policy=chain.instinct_policy,
            instinct_policy_passed=chain.instinct_policy_passed,
            precedents=list(chain.precedent_hints),  # A3 path 1
            outcome=outcome,
            payload=chain.payload_acc,
            pocket_id=chain.pocket_id,
            correlation_id=chain.correlation_id,
            last_seq=chain.last_seq,
        )

        # G6 hash chain — include prev_hash within correlation_id.
        prev = self._last_hash_per_correlation.get(chain.correlation_id, "")
        decision.hash_link = compute_hash_link(decision, prev)
        self._last_hash_per_correlation[chain.correlation_id] = decision.hash_link

        # A3 path 2: precedent fallback if payload empty. Only attempt
        # when the agent didn't supply ANY precedents and the chain has
        # a pocket_id to scope the lookup.
        if not decision.precedents and decision.pocket_id and not rejected:
            fallback = self._fallback_precedents(decision)
            decision.precedents = fallback

        edges = self._build_edges(decision)
        self._store.upsert_decision(decision, edges=edges)

        self._pending.pop(chain.correlation_id, None)
        return decision

    def _emit_degenerate(self, entry: EventEntry) -> Decision:
        """A one-off fabric write with no correlation — emit a minimal
        Decision so the graph still has a node for it (RFC line 274)."""
        decision_id = uuid4()
        payload = entry.payload or {}
        obj_id = payload.get("object_id", "unknown")
        decision = Decision(
            id=decision_id,
            ts=entry.ts,
            decided_by=entry.actor,
            scope=list(entry.scope),
            scope_kind=_infer_scope_kind(list(entry.scope), None),
            intent=f"Direct write to {obj_id}",
            action=entry.action,
            inputs=[InputRef(kind="fabric_object", id=str(obj_id), label=str(obj_id))],
            payload=dict(payload),
            correlation_id=None,
            last_seq=(getattr(entry, "seq", None) or 0),
        )
        decision.hash_link = compute_hash_link(decision, "")
        edges = self._build_edges(decision)
        self._store.upsert_decision(decision, edges=edges)
        return decision

    def _apply_outcome_attached(self, entry: EventEntry) -> Decision | None:
        """Mutate the outcome on an existing Decision. Hash chain is
        preserved (outcome isn't in the hash material)."""
        payload = entry.payload or {}
        decision_id_raw = payload.get("decision_id")
        if not decision_id_raw:
            logger.warning("outcome_attached without decision_id — dropped")
            return None
        try:
            decision_id = UUID(decision_id_raw)
        except (ValueError, TypeError):
            logger.warning(
                "outcome_attached with invalid decision_id %r — dropped",
                decision_id_raw,
            )
            return None

        existing = self._store.get_decision(decision_id)
        if existing is None:
            logger.warning("outcome_attached for unknown decision %s — dropped", decision_id)
            return None

        outcome_id_raw = payload.get("outcome_id")
        try:
            outcome_id = UUID(outcome_id_raw) if outcome_id_raw else uuid4()
        except (ValueError, TypeError):
            outcome_id = uuid4()

        outcome = OutcomeRef(
            outcome_id=outcome_id,
            status=payload.get("status", "landed"),
            landed_at=_parse_dt(payload.get("landed_at")) or entry.ts,
            metered=bool(payload.get("metered", False)),
        )
        self._store.update_outcome(decision_id, outcome)
        # Return the refreshed Decision so callers can act on it.
        return self._store.get_decision(decision_id)

    # --- edges --------------------------------------------------------------

    def _build_edges(self, decision: Decision) -> list[DecisionEdgeRecord]:
        """Build the edge rows the Decision contributes to the graph."""
        edges: list[DecisionEdgeRecord] = []
        # precedent edges (forward; downstream queries read these inversely)
        for p in decision.precedents:
            edges.append(
                DecisionEdgeRecord(
                    src_id=decision.id,
                    target_id=str(p.decision_id),
                    relation="precedent",
                    weight=p.weight,
                )
            )
        # input edges
        for inp in decision.inputs:
            edges.append(
                DecisionEdgeRecord(
                    src_id=decision.id,
                    target_id=inp.id,
                    relation="input",
                    weight=1.0,
                )
            )
        # approval edges (one per approver — actor id as the target)
        for app in decision.approvers:
            edges.append(
                DecisionEdgeRecord(
                    src_id=decision.id,
                    target_id=app.actor.id,
                    relation="approval",
                    weight=1.0,
                )
            )
        # outcome edge — only emitted when an outcome is present at first
        # emit (rejection). Late-attach uses store.update_outcome which
        # writes the edge separately.
        if decision.outcome is not None:
            edges.append(
                DecisionEdgeRecord(
                    src_id=decision.id,
                    target_id=str(decision.outcome.outcome_id),
                    relation="outcome",
                    weight=1.0,
                )
            )
        return edges

    def _fallback_precedents(self, decision: Decision) -> list[DecisionRef]:
        """A3 path 2: same-pocket + same-action + nearest-ts (top 3).
        Weights decay 0.95 → 0.85 → 0.75 by recency rank."""
        if not decision.pocket_id:
            return []
        siblings = [
            d
            for d in self._store.iter_decisions(
                pocket_id=decision.pocket_id,
            )
            if d.id != decision.id and d.action == decision.action and d.ts < decision.ts
        ]
        # iter_decisions already sorts by ts DESC — take the top 3.
        out: list[DecisionRef] = []
        for i, sib in enumerate(siblings[:3]):
            weight = round(0.95 - (i * 0.1), 2)
            out.append(
                DecisionRef(
                    decision_id=sib.id,
                    relation="precedent",
                    weight=weight,
                )
            )
        return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_dt(v: Any) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _infer_scope_kind(scope: list[str], pocket_id: str | None) -> str:
    if pocket_id:
        return "pocket"
    for s in scope:
        if s.startswith("pocket:"):
            return "pocket"
        if s.startswith("team:"):
            return "team"
        if s.startswith("org:"):
            return "org"
    return "workspace"


__all__ = ["DecisionProjection"]
