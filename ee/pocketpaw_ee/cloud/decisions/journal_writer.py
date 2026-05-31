# journal_writer.py — Canonical write path for Decision-Graph chain events.
# Created: 2026-05-25 (RFC 09 Slice 1b — feat/rfc-09-slice-1-record-decision-event)
#
#   Purpose
#   -------
#   RFC 09 (paw-workspace#63) introduces 9 producer sites across 4
#   production files that emit the chain-forming actions
#   (`agent.proposed`, `human.corrected`, `policy.evaluated`,
#   `decision.completed`). Every producer must do the same two-step:
#
#       1. `journal.append(entry)` — make the event the durable record
#       2. `projection.apply(entry)` — fold the event into the live store
#
#   Doing it wrong (skipping the journal, swapping the order, swallowing
#   exceptions inconsistently) creates either a stuck projection or a
#   silent gap between the audit log and the Decision Graph. RFC 09
#   Audit Open Question 11 calls this out specifically — the synthetic
#   `decision.outcome_attached` path bypasses `journal.append` and so
#   never advances the projection cursor; we don't want any chain-forming
#   producer to repeat that mistake.
#
#   `record_decision_event(...)` is the single co-location helper. Slices
#   2 + 3 wire each producer site through this function instead of calling
#   `journal.append` directly.
#
#   2026-05-25 (RFC 09 Slice 2) — Producers call the per-action wrappers
#   at the bottom of this file (``record_agent_proposed``,
#   ``record_policy_evaluated``, ``record_human_corrected``,
#   ``record_decision_completed``) so the chain-action string literal
#   does not appear at producer call sites — keeps the
#   ``scripts/audit_decision_chain.py`` lint clean. Each wrapper is a
#   one-line forwarder to ``record_decision_event``.
#
#   Soul-protocol version note
#   --------------------------
#   Soul-protocol 0.3.1 (the wheel currently installed) does NOT yet have
#   `build_policy_event` / `build_completion_event` / `PolicyEvaluation`
#   / `DecisionCompletion`. Those land in Slice 1a (soul-protocol release
#   bumping to 0.4.x). Until that wheel is published, this helper
#   constructs `EventEntry` instances directly with string-literal action
#   names. The `# TODO(rfc09-slice-1a-wheel)` markers below flag the
#   call sites that should switch to the new builders once they exist.
#
#   `decision.completed` is similarly absent from soul-protocol's
#   ACTION_NAMESPACES registry today (which lists `decision.graduated`
#   under the old "pattern-promotion" meaning). The journal does NOT
#   validate `entry.action` against ACTION_NAMESPACES — appending
#   `decision.completed` is accepted right now and will continue to be
#   accepted when Slice 1a registers it formally. RFC 09 § "Vocabulary
#   fix" pins this rename.
#
#   Failure isolation
#   -----------------
#   Per RFC 09 § "Architecture — three layers in concert", the journal
#   write is the source of truth. If `projection.apply` raises (a bug, a
#   schema drift, transient I/O), the helper logs a warning and returns
#   the EventEntry anyway — the producer's request must NOT fail because
#   of a projection problem. The Slice 4 reconciler will pick up any
#   apply-failed entries on its next pass since the journal row exists.
#
#   Idempotency
#   -----------
#   The helper does NOT dedupe on `correlation_id` or `event_id`. The
#   journal itself enforces seq-monotonic append; the projection's
#   `apply()` is idempotent on (correlation_id, action) when the same
#   payload lands twice via rebuild + hot-path. If a caller wants
#   no-duplicate semantics it MUST check before calling — for instance,
#   `run_action`'s re-entry path uses `from_instinct=True` to suppress
#   the second `agent.proposed` (RFC 09 audit Surprise 3).

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from soul_protocol.spec.journal import Actor, EventEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action namespace — the chain-forming actions producers route through here
# ---------------------------------------------------------------------------
# Membership is the trip-wire for the lint contract in ee/pyproject.toml:
# only `journal_writer.py` may import the soul-protocol builders that
# construct these specific actions. Producers outside this module call
# `record_decision_event` instead. The set is a frozenset so a producer
# can do an `action in DECISION_CHAIN_ACTIONS` membership check without
# fear of mutation.
#
# Notes on what's IN and what's OUT:
#   * `agent.proposed` / `human.corrected` / `policy.evaluated` /
#     `decision.completed` — yes, these are the chain spine. Every emit
#     MUST go through `record_decision_event`.
#   * `decision.outcome_attached` is intentionally NOT here. It is a
#     mutation-only event emitted from the outcomes service AFTER a chain
#     has closed; it doesn't open or close a chain, so it doesn't carry
#     the same "must journal + apply in lockstep" risk profile. The
#     outcomes service has its own back-reference contract (RFC 07
#     Slice 2) and lives in `outcomes/service.py`.
#   * `fabric.object.*` events are CONTRIBUTING (they add inputs) but
#     not terminal — they're emitted by `FabricJournalStore` via its own
#     well-established co-location pattern (`src/pocketpaw/fabric/
#     journal_store.py`). RFC 07 § A2 amendment makes the chain stay
#     open until `decision.completed` lands, so fabric writes never
#     close a chain on their own.
DECISION_CHAIN_ACTIONS: frozenset[str] = frozenset(
    {
        "agent.proposed",
        "human.corrected",
        "policy.evaluated",
        "decision.completed",  # RFC 09 § Vocabulary fix — was "decision.graduated"
    }
)


# ---------------------------------------------------------------------------
# Helper — the single chokepoint
# ---------------------------------------------------------------------------


def record_decision_event(
    *,
    action: str,
    correlation_id: UUID,
    actor: Actor,
    scope: list[str],
    payload: dict | None = None,
    causation_id: UUID | None = None,
    ts: datetime | None = None,
    event_id: UUID | None = None,
) -> EventEntry:
    """Append a Decision-Graph chain event AND fold it into the projection.

    Canonical co-location for the four chain-forming actions in
    `DECISION_CHAIN_ACTIONS`. Producers in Slices 2 + 3 (agent runtime,
    Instinct evaluator, approve/reject endpoints, chain-close moments)
    MUST call this function instead of `journal.append(entry)` directly.

    Contract (load-bearing — see RFC 09 § Architecture):
      1. Build EventEntry from the kwargs.
      2. `journal.append(entry)` — durable write FIRST so the audit log
         is consistent even if the projection's fold raises.
      3. `get_decision_graph().projection.apply(entry)` — fold the entry
         into the live store so queries / explain narrators see it
         without waiting for the Slice 4 reconciler.

    Ordering matters. Reversing the calls would advance the projection
    cursor over an event that the journal does not yet hold; a crash
    between the two would leave the projection ahead of the journal and
    the next rebuild would miss the row. The helper enforces the
    journal-first order so producers don't have to remember.

    Failure isolation
    -----------------
    `projection.apply()` failures are logged as warnings and swallowed —
    the EventEntry is still returned to the caller. The journal row is
    the source of truth; the projection can be reconstructed at any time
    via `projection.rebuild(journal, since_seq=...)`. Letting a fold bug
    block the producer's request would couple every Instinct approval
    to the Decision Graph's health, which is the opposite of what RFC 09
    wants.

    `journal.append()` failures DO propagate. If the audit log can't be
    written, that's a real problem the producer needs to know about.

    Args:
        action: One of `DECISION_CHAIN_ACTIONS`. Passing something else
            still works (the journal accepts arbitrary action strings),
            but the projection will drop it and the lint contract exists
            to nudge callers toward `journal.append` directly for
            non-chain events.
        correlation_id: The chain id. Same id flows from
            `agent.proposed` through to `decision.completed` so the
            projection folds them into one Decision row.
        actor: The actor responsible for the event. Carries the org /
            workspace scope context the projection uses for visibility
            filters.
        scope: Tenancy tags — typically `[f"pocket:{pocket_id}",
            f"workspace:{workspace_id}"]`. Required by the journal
            invariant (min_length=1).
        payload: Action-specific payload. For `agent.proposed` it's an
            `AgentProposal.model_dump(mode="json")` dict; for
            `human.corrected` it's a `HumanCorrection` dict; for
            `policy.evaluated` it's the policy-evaluation shape that
            Slice 1a will pin as `PolicyEvaluation`; for
            `decision.completed` it's the chain-close shape that Slice
            1a will pin as `DecisionCompletion`.
        causation_id: Optional pointer to the event that directly caused
            this one (e.g. `human.corrected` cites the prior
            `policy.evaluated(passed=False)` event id).
        ts: Optional timestamp override. Defaults to `datetime.now(UTC)`
            — the journal validates that ts is timezone-aware UTC.
        event_id: Optional event id override. Defaults to `uuid4()`.

    Returns:
        The freshly-constructed EventEntry. Producers usually don't need
        it but some sites stash the entry id on a parked blob (RFC 09
        Slice 3 wires `human.corrected.causation_id` from the prior
        `policy.evaluated` entry).
    """
    # Lazy import — the decisions service is in the same package, but
    # leaving the import at function-scope keeps a clean dependency
    # picture in static analysis and avoids touching the
    # `init_decisions_projection` singleton at module-import time.
    from pocketpaw.journal_dep import get_journal
    from pocketpaw_ee.cloud.decisions.service import get_decision_graph

    entry = EventEntry(
        # TODO(rfc09-slice-1a-wheel): when soul-protocol publishes
        # `build_policy_event` / `build_completion_event`, route the
        # `policy.evaluated` and `decision.completed` calls through them
        # so the PolicyEvaluation / DecisionCompletion payload validation
        # runs at construction time. `agent.proposed` and
        # `human.corrected` can also move to `build_proposal_event` /
        # `build_correction_event` (already shipped in 0.3.1) — they
        # accept a structured AgentProposal / HumanCorrection rather
        # than a raw dict, so producers will need to construct those
        # models first.
        id=event_id or uuid4(),
        ts=ts or datetime.now(UTC),
        actor=actor,
        action=action,
        scope=list(scope),
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload or {},
    )

    journal = get_journal()
    # Journal.append returns None — seq lives only in the backend. The
    # projection's apply() handles a missing seq attribute by falling
    # back to 0, which is fine for hot-path folds (the cursor doesn't
    # need to advance for the fold itself; the Slice 4 reconciler uses
    # the journal's real seq when replaying from the cursor).
    journal.append(entry)

    try:
        graph = get_decision_graph()
        graph.projection.apply(entry)
    except Exception:  # noqa: BLE001 — see "Failure isolation" above
        logger.warning(
            "decisions projection.apply raised for %s (correlation_id=%s) — "
            "journal row written, projection will catch up via Slice 4 "
            "reconciler",
            action,
            correlation_id,
            exc_info=True,
        )

    return entry


# ---------------------------------------------------------------------------
# Per-action convenience wrappers (RFC 09 Slice 2)
# ---------------------------------------------------------------------------
# Producer sites in Slices 2 + 3 call the wrappers below instead of
# spelling the chain-action strings at the call site. Two reasons:
#
#   1. The ``scripts/audit_decision_chain.py`` lint script flags any
#      occurrence of ``action="agent.proposed"`` (et al) outside this
#      module. Hiding the literal behind a wrapper keeps producer files
#      audit-clean without losing the no-direct-construction guarantee.
#
#   2. A future swap to soul-protocol's typed builders (``build_proposal_event``
#      with a structured ``AgentProposal`` payload, etc. — currently
#      gated on the Slice 1a wheel publishing) only touches these four
#      wrappers. Producer call sites stay the same.
#
# Each wrapper has the same kwargs as ``record_decision_event`` minus
# the ``action`` string itself. ``payload`` shape is whatever the
# projection's ``_fold_<kind>`` reads — see the docstrings on the
# wrappers for the expected keys.


def record_agent_proposed(
    *,
    correlation_id: UUID,
    actor: Actor,
    scope: list[str],
    payload: dict,
    causation_id: UUID | None = None,
    ts: datetime | None = None,
    event_id: UUID | None = None,
) -> EventEntry:
    """Emit ``agent.proposed`` — the chain-opening event.

    Fold target: ``DecisionProjection._fold_proposed`` reads
    ``intent`` / ``action`` / ``pocket_id`` / ``inputs`` /
    ``precedents`` / ``data`` off the payload.

    Producers: pocket runtime (``action_executor.run_action`` after the
    allowlist gate, gated by ``not from_instinct`` — RFC 09 audit
    Surprise 3) and any future "agent proposed a tool call" site.
    """
    return record_decision_event(
        action="agent.proposed",
        correlation_id=correlation_id,
        actor=actor,
        scope=scope,
        payload=payload,
        causation_id=causation_id,
        ts=ts,
        event_id=event_id,
    )


def record_policy_evaluated(
    *,
    correlation_id: UUID,
    actor: Actor,
    scope: list[str],
    payload: dict,
    causation_id: UUID | None = None,
    ts: datetime | None = None,
    event_id: UUID | None = None,
) -> EventEntry:
    """Emit ``policy.evaluated`` — a policy gate observation.

    Fold target: ``DecisionProjection._fold_policy`` reads
    ``policy`` / ``passed`` / ``reason`` off the payload.

    Producers:
      * direct-path auto-approve (Slice 2 — ``action_executor`` at the
        gate-8 success branch) — ``passed=True, policy="auto"``.
      * Instinct park (Slice 3 — ``instinct_bridge.propose_pocket_write``
        after the Action is stored) — ``passed=False,
        reason="parked_for_human_approval"``.
      * Instinct approve (Slice 3 — ``instinct/router.approve_action``
        after ``store.approve``) — ``passed=True``.
    """
    return record_decision_event(
        action="policy.evaluated",
        correlation_id=correlation_id,
        actor=actor,
        scope=scope,
        payload=payload,
        causation_id=causation_id,
        ts=ts,
        event_id=event_id,
    )


def record_human_corrected(
    *,
    correlation_id: UUID,
    actor: Actor,
    scope: list[str],
    payload: dict,
    causation_id: UUID | None = None,
    ts: datetime | None = None,
    event_id: UUID | None = None,
) -> EventEntry:
    """Emit ``human.corrected`` — a human approver's disposition.

    Fold target: ``DecisionProjection._fold_corrected`` appends an
    ``ApproverRef`` and stashes any ``note`` from the payload.

    Producers (Slice 3, all in ``ee/pocketpaw_ee/instinct/router.py``):
    ``approve_action`` (disposition ``accepted``/``edited``),
    ``reject_action`` (``rejected``), and the bulk variants of both.
    """
    return record_decision_event(
        action="human.corrected",
        correlation_id=correlation_id,
        actor=actor,
        scope=scope,
        payload=payload,
        causation_id=causation_id,
        ts=ts,
        event_id=event_id,
    )


def record_decision_completed(
    *,
    correlation_id: UUID,
    actor: Actor,
    scope: list[str],
    payload: dict,
    causation_id: UUID | None = None,
    ts: datetime | None = None,
    event_id: UUID | None = None,
) -> EventEntry:
    """Emit ``decision.completed`` — the chain-closing terminal event.

    Fold target: ``DecisionProjection._close_chain`` reads ``passed``
    off the payload; ``action_outcome`` / ``error_class`` / ``reason``
    ride along for the explain narrator.

    Producers:
      * direct success / failure (Slice 2 — ``action_executor`` at
        gate-8 success branch and the 4 except branches; gated by
        ``not binding.requires_instinct`` so the Instinct re-entry
        doesn't double-emit).
      * Instinct re-entry success / failure (Slice 2 —
        ``instinct_bridge.execute_approved_write`` after
        ``store.mark_executed`` or on rejection / executor crash).
      * Instinct reject (Slice 3 — ``instinct/router.reject_action``).
      * Abandon path (Slice 4 — ``decisions._action_sweeper`` for
        chains stuck >24h without a terminal).
    """
    return record_decision_event(
        action="decision.completed",
        correlation_id=correlation_id,
        actor=actor,
        scope=scope,
        payload=payload,
        causation_id=causation_id,
        ts=ts,
        event_id=event_id,
    )


__all__ = [
    "DECISION_CHAIN_ACTIONS",
    "record_agent_proposed",
    "record_decision_completed",
    "record_decision_event",
    "record_human_corrected",
    "record_policy_evaluated",
]
