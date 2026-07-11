# ee/cloud/fabric_conflicts/propose.py — stage an un-rankable Fabric conflict for a steward.
# Created: 2026-07-10 (FST-6 — the conflict lifecycle: _fabric_conflict proposal type).
#
# What this module does (the propose half of the conflict-stewardship gate): the
# source-truth chain's resolver (FST-2) auto-resolves every conflict it can RANK;
# the残り — same-tier, same-rank, both-open, materially-different, within-epsilon
# conflicts (``Resolution.unresolvable=True``) — cannot be ordered by policy and
# need a human. This module turns each such open conflict (recomputed from
# statements by ``pocketpaw.fabric.conflicts.detect_open_conflicts`` — no
# conflicts table) into ONE Instinct ``Action`` carrying a ``_fabric_conflict``
# blob (schema 1) under ``Action.parameters``. A human arbitrates it in The Tray:
# approve (optionally editing the choice) → the apply-on-approve executor
# (``fabric_conflicts.executor.execute_approved_fabric_conflict``) PINs the
# chosen statement via the canonical OSS steward verb
# ``FabricStore.pin_statement``; reject → the policy's provisional winner stands,
# NO statement changes (the router owns the reject-close). This module does NOT
# write Fabric — it only gates.
#
# PRECEDENT MIRRORED: ``instinct_rule_proposals`` (propose.py stages one proposal
# per subject, executor.py fires on approve, the router owns reject-close) — the
# closest shape: one SUBJECT (here one conflicted property) per proposal, an
# editable sub-dict the human may adjust before approving, tenancy pinned on
# SEPARATE top-level blob fields. The blob sits alongside ``_instinct_rule``,
# ``_fabric_objects``, ``_pocket_create``, ``_pocket_write``, ``_code_change``,
# ``_external_action``, ``_belt_plan``, ``_artifact_change``, ``_admin_action``;
# the router + executor dispatch on the ``_fabric_conflict`` parameters key.
#
# Schema 1 — the blob carries:
#   * ``schema`` / ``kind`` — version + discriminator;
#   * ``workspace_id`` — the originating tenant, a SEPARATE top-level field (NOT
#     inside the editable ``resolution``) so an edit can never move the proposal
#     to another workspace. The executor's tenancy gate reads it here; the PIN is
#     executed against this workspace's Fabric store.
#   * ``object_id`` / ``property`` — the conflict subject. Top-level (immutable
#     via edits); together with ``workspace_id`` they form the DEDUPE KEY
#     ``(workspace_id, object_id, property)`` — the sweep guarantees at most ONE
#     open proposal per key.
#   * ``object_type`` — display context for the Tray.
#   * ``choices`` — the competing statements (the policy's provisional winner
#     first, then the un-rankable rivals), each with value + provenance
#     (writer_class, rank, observed_at/recorded_at, and the SourceRef fields —
#     enough for a human to decide). Read-only context.
#   * ``policy_winner_statement_id`` — what policy provisionally picked (what
#     "reject" keeps).
#   * ``conflict_signature`` — sorted competing statement ids. Reject-memory: a
#     rejected proposal for the same key with the SAME signature blocks
#     re-filing (the human already said "keep the policy winner" for exactly
#     this conflict); a new rival observation changes the signature and the
#     conflict is re-staged.
#   * ``resolution`` — the ONE editable sub-dict:
#     ``{"chosen_statement_id": <id>}``, defaulting to the policy winner. The
#     human either approves as-is (endorse policy's pick — now durable) or edits
#     the choice to a rival via the approve-with-edits path
#     (``ApproveRequest.parameters``) before approving. The executor validates
#     the chosen id against ``choices`` — an edit cannot smuggle in an arbitrary
#     statement.
#   * ``summary`` — a human-readable one-liner for the gate UI;
#   * ``correlation_id`` / ``proposed_event_id`` — the Decision-Graph chain ids
#     (``agent.proposed`` opens the chain; the router's approve/reject paths and
#     the executor close the SAME chain).
#
# APPROVE-CHOICE → VERB MAPPING (settled here, per the FST-6 design): approving
# with choice A **PINs A's statement** — the durable "this one wins". PIN beats
# IGNORE-the-rival because (a) the resolver's pinned short-circuit also settles
# FUTURE rival observations, not just today's, and (b) the losing statements
# stay intact for audit instead of being struck. IGNORE remains available as a
# direct OSS steward verb for genuinely bogus claims.
#
# SWEEP WIRING (the lightest honest trigger): ``sweep_conflicts_to_proposals``
# is invoked at the tail of ``fabric_ingest.service.run_ingest_sweep`` for each
# workspace the ingest touched — conflicts are born at merge sites, so
# after-ingest is the natural beat, AND that sweep already runs every 5 minutes
# under ``FabricIngestScheduler`` (the established cloud periodic pattern), so
# the same hook is the periodic sweep too. No new scheduler class. Best-effort,
# exception-shielded, mode-gated (off → zero reads).
#
# Mode gate: the lifecycle runs in shadow AND enforce — a queued conflict in
# shadow is observation with a human answer (the PIN lands on the statement
# layer; only the cache write is enforce-only, exactly the verbs' contract).
# Off → nothing (no scans, no proposals).
#
# Volume guard (the PRD queue-volume metric): after each sweep the number of
# OPEN stewardship proposals for the workspace is counted; past
# ``STEWARDSHIP_QUEUE_WARN_THRESHOLD`` (5) a grep-stable warning line is logged
# — conflicts outpacing steward review is a rules/trust-ladder problem, not
# something to silently queue.
#
# Security:
#   * NO Fabric mutation here — the conflict is staged as DATA; the PIN only
#     lands after a human approves.
#   * Tenancy + subject are SEPARATE top-level blob fields (NOT in the editable
#     ``resolution``); the router's ``_assert_fabric_conflict_workspace`` gates
#     all four approve/reject paths (asymmetric tenant scope is no tenant scope
#     — pocketpaw#1183 / #1250) and the executor re-validates.

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# The Instinct Action kind discriminator for a conflict-stewardship proposal.
# The router + executor dispatch on the presence of the parameters key below;
# the blob also carries ``kind="fabric_conflict"`` for readers that introspect it.
FABRIC_CONFLICT_KIND = "fabric_conflict"

# The parameters key under which the conflict blob rides — peer of
# ``_instinct_rule`` / ``_fabric_objects`` / ``_pocket_create`` et al.
FABRIC_CONFLICT_PARAM_KEY = "_fabric_conflict"

# Schema version stamped on the ``_fabric_conflict`` blob. Bump when the blob
# shape changes so a stale pending Action approved after a deploy fails loud
# instead of pinning a misinterpreted statement. Starts at 1 — first version.
FABRIC_CONFLICT_SCHEMA = 1

# The PRD queue-volume metric: past this many OPEN stewardship proposals in one
# workspace, the sweep logs a warning — un-rankable conflicts are outpacing
# steward review.
STEWARDSHIP_QUEUE_WARN_THRESHOLD = 5


def _source_truth_mode() -> str:
    """The fabric_source_truth_mode setting, read through the OSS chokepoint.

    Late import + late call so a test monkeypatch of
    ``pocketpaw.fabric.store._source_truth_mode`` (the suite-wide seam every
    FST test uses) is observed here too — ONE definition of the mode read.
    """
    from pocketpaw.fabric import store as fabric_store_mod

    return fabric_store_mod._source_truth_mode()


def _emit_agent_proposed(
    *,
    correlation_id: UUID,
    action_id: str,
    workspace_id: str,
    user_id: str,
    object_type: str,
    property: str,
    choice_count: int,
) -> UUID | None:
    """Emit the chain-opening ``agent.proposed`` event for a conflict proposal.

    Mirrors ``instinct_rule_proposals.propose._emit_agent_proposed``: the
    proposing caller is the actor (``kind="agent"``); a conflict isn't bound
    to a pocket — its tenancy is the workspace — so ``pocket_id`` on the chain
    carries the workspace id (matching the Action's ``pocket_id``).

    Returns the emitted event id (back-written onto the blob for the
    ``human.corrected`` causation chain) or ``None`` when the emit raised —
    best-effort per RFC 09; the Slice 4 reconciler picks up orphans.
    """
    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_agent_proposed

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}"],
    )
    intent = f"arbitrate {choice_count} competing values for {object_type or 'object'}.{property}"
    payload: dict[str, Any] = {
        # Fields the projection's ``_fold_proposed`` consumes.
        "intent": intent,
        "action": "fabric_conflict",
        "pocket_id": workspace_id,
        "inputs": [],
        # Richer fields for the explain narrator.
        "proposal_kind": "fabric_conflict",
        "proposal": {
            "object_type": object_type,
            "property": property,
            "choice_count": choice_count,
        },
        "action_id": action_id,
    }
    try:
        entry = record_agent_proposed(
            correlation_id=correlation_id,
            actor=actor,
            scope=[f"workspace:{workspace_id}"],
            payload=payload,
        )
        return entry.id
    except Exception:  # noqa: BLE001 — chain emit is best-effort
        logger.warning(
            "fabric_conflict agent.proposed emit failed for correlation_id=%s "
            "(action_id=%s) — Slice 4 reconciler will catch up",
            correlation_id,
            action_id,
            exc_info=True,
        )
        return None


async def _persist_chain_ids(
    *,
    store: Any,
    action_id: str,
    correlation_id: str,
    proposed_event_id: str | None,
) -> None:
    """Back-write ``correlation_id`` + ``proposed_event_id`` onto the persisted
    Action's ``parameters._fabric_conflict`` blob after ``agent.proposed`` fired.

    Direct SQL update — the same pattern the instinct-rule gate's
    ``_persist_chain_ids`` uses. Best-effort: a write failure leaves
    ``proposed_event_id`` None and the eventual ``human.corrected`` emits
    without a causation_id (the chain still folds).
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(FABRIC_CONFLICT_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["correlation_id"] = correlation_id
        blob["proposed_event_id"] = proposed_event_id
        params[FABRIC_CONFLICT_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — write-back is best-effort
        logger.warning(
            "fabric_conflict: failed to persist chain ids onto action %s — the "
            "chain's human.corrected will emit without causation_id",
            action_id,
            exc_info=True,
        )


def _short(value: Any, limit: int = 60) -> str:
    """A compact display form of a statement value for titles/summaries."""
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _statement_choice(fabric: Any, stmt: Any, workspace_id: str) -> dict[str, Any]:
    """One entry of the blob's ``choices`` list: value + full provenance.

    Enough for a human to decide: WHO wrote it (writer_class), WHEN the
    source says it was true (observed_at), when we learned it (recorded_at),
    and WHERE it came from (the SourceRef identity fields). The source lookup
    is best-effort — a missing SourceRef (pruned, out of scope) degrades to
    ``source: None`` rather than blocking the proposal.
    """
    source_payload: dict[str, Any] | None = None
    try:
        source = await fabric.get_source(stmt.source_ref_id, workspace_id=workspace_id or None)
    except Exception:  # noqa: BLE001 — provenance display is best-effort
        source = None
    if source is not None:
        source_payload = {
            "kind": source.kind,
            "connector": source.connector,
            "run_id": source.run_id,
            "document_uri": source.document_uri,
            "actor_id": source.actor_id,
            "session_id": source.session_id,
        }
    return {
        "statement_id": stmt.id,
        "value": stmt.value,
        "writer_class": stmt.writer_class,
        "rank": stmt.rank,
        "pinned": stmt.pinned,
        "observed_at": stmt.observed_at.isoformat(),
        "recorded_at": stmt.recorded_at.isoformat(),
        "source": source_payload,
    }


async def propose_fabric_conflict(
    *,
    workspace_id: str,
    conflict: Any,
    requested_by: str = "fabric_steward",
    summary: str | None = None,
    correlation_id: str | None = None,
    assignee: str | None = None,
    fabric_store: Any | None = None,
) -> str:
    """Build + store an Instinct ``Action`` for ONE un-rankable conflict.

    ``conflict`` is a ``pocketpaw.fabric.conflicts.ConflictRecord`` (winner +
    rivals). Files an Action carrying the ``_fabric_conflict`` blob (schema 1)
    and opens the Decision-Graph chain. Returns the proposed Action id. NO
    Fabric mutation happens here — the choice is staged as DATA and the PIN
    only lands after a human approves.

    Callers normally reach this through :func:`sweep_conflicts_to_proposals`
    (which dedupes); calling it directly bypasses the one-open-per-key
    guarantee.
    """
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_fabric_store, get_instinct_store

    workspace_id = str(workspace_id or "")
    if not workspace_id:
        raise ValueError("propose_fabric_conflict requires a non-empty workspace_id")
    if conflict.winner is None or not conflict.rivals:
        raise ValueError("propose_fabric_conflict requires a conflict with a winner and rivals")

    fabric = fabric_store or get_fabric_store(workspace_id=workspace_id or None)

    competing = [conflict.winner, *conflict.rivals]
    choices = [await _statement_choice(fabric, s, workspace_id) for s in competing]

    corr = correlation_id or str(uuid4())

    values_text = " vs ".join(_short(c["value"]) for c in choices)
    subject = f"{conflict.object_type or 'object'}.{conflict.property}"
    human_summary = summary or (
        f"Policy cannot rank {len(choices)} competing values for {subject}"
        f" ({values_text}). Approve to make the selected value the durable"
        f" winner (PIN); reject to keep the policy winner."
    )

    blob: dict[str, Any] = {
        "kind": FABRIC_CONFLICT_KIND,
        "schema": FABRIC_CONFLICT_SCHEMA,
        # Tenancy + subject are SEPARATE top-level fields (NOT in the editable
        # ``resolution``) so an edit can never re-scope or re-target the pin.
        "workspace_id": workspace_id,
        "object_id": conflict.object_id,
        "property": conflict.property,
        "object_type": conflict.object_type or "",
        # Read-only decision context.
        "choices": choices,
        "policy_winner_statement_id": conflict.winner.id,
        "conflict_signature": list(conflict.signature),
        # The ONE editable sub-dict — the human's choice (defaults to the
        # policy winner; approving unedited endorses policy's pick durably).
        "resolution": {"chosen_statement_id": conflict.winner.id},
        "summary": human_summary,
        # RFC 09 chain-correlation fields (schema 1 carries them from the start).
        "correlation_id": corr,
        "proposed_event_id": None,
    }

    title = f"Disputed fact — {subject} has {len(choices)} competing values"
    recommendation = f"Pick the value that should win. {human_summary}"
    trigger = ActionTrigger(
        type="agent",
        source=requested_by or "fabric_conflicts",
        reason="un-rankable Fabric conflict requires a steward decision",
    )

    # ISO: scope the store to the caller's workspace (validated non-empty
    # above) — this propose path has no ``current_workspace`` ContextVar set.
    store = get_instinct_store(workspace_id=workspace_id or None)
    action_obj = await store.propose(
        # ``pocket_id`` carries the workspace — a conflict isn't bound to a
        # pocket (mirrors the instinct-rule gate); the workspace also rides on
        # the blob (the executor's tenancy gate reads it there).
        pocket_id=workspace_id,
        title=title,
        description=recommendation,
        recommendation=recommendation,
        trigger=trigger,
        category=ActionCategory.WORKFLOW,
        priority=ActionPriority.MEDIUM,
        parameters={FABRIC_CONFLICT_PARAM_KEY: blob},
        assignee=assignee or None,
        workspace_id=workspace_id,
    )

    logger.info(
        "fabric_conflict: proposed stewardship for %s (object=%s, %d choices) → "
        "Instinct action %s (workspace=%s, correlation_id=%s)",
        subject,
        conflict.object_id,
        len(choices),
        action_obj.id,
        workspace_id,
        corr,
    )

    # Open the Decision-Graph chain now that the Action is stored. Best-effort:
    # a Decision-Graph wiring failure must NOT fail the propose.
    proposed_event_id = _emit_agent_proposed(
        correlation_id=UUID(corr),
        action_id=action_obj.id,
        workspace_id=workspace_id,
        user_id=requested_by,
        object_type=conflict.object_type or "",
        property=conflict.property,
        choice_count=len(choices),
    )
    if proposed_event_id is not None:
        await _persist_chain_ids(
            store=store,
            action_id=action_obj.id,
            correlation_id=corr,
            proposed_event_id=str(proposed_event_id),
        )

    return action_obj.id


def _conflict_blob(action: Any) -> dict[str, Any] | None:
    """The ``_fabric_conflict`` blob on an Action, or ``None``."""
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get(FABRIC_CONFLICT_PARAM_KEY)
    return blob if isinstance(blob, dict) else None


def _blob_dedupe_key(blob: dict[str, Any]) -> tuple[str, str, str]:
    """The proposal's dedupe key off the blob's top-level subject fields."""
    return (
        str(blob.get("workspace_id") or ""),
        str(blob.get("object_id") or ""),
        str(blob.get("property") or ""),
    )


async def sweep_conflicts_to_proposals(
    workspace_id: str,
    *,
    requested_by: str = "fabric_steward",
    assignee: str | None = None,
    fabric_store: Any | None = None,
) -> list[str]:
    """Turn one workspace's open un-rankable conflicts into Instinct proposals.

    The sweep half of the FST-6 lifecycle (see the module header for the
    wiring: post-ingest hook + the 5-minute ingest scheduler beat). Steps:

    1. Mode gate — ``fabric_source_truth_mode`` off → return [] with ZERO
       reads. Shadow and enforce both sweep (a queued conflict in shadow is
       observation with a human answer).
    2. Recompute the open conflicts from statements
       (``detect_open_conflicts`` — no persisted conflict state).
    3. Dedupe — ONE open proposal per ``(workspace_id, object_id, property)``:
       a key with a PENDING ``_fabric_conflict`` Action is skipped, so
       re-sweeps never duplicate while one is open (the instinct-rule
       precedent's one-proposal-per-subject discipline). Additionally,
       reject-memory: a key whose most recent REJECTED proposal carries the
       SAME ``conflict_signature`` is skipped — the human already answered
       "keep the policy winner" for exactly this conflict; a new rival
       observation changes the signature and re-stages it.
    4. File a proposal per remaining conflict (isolated — one failure never
       blocks the rest).
    5. Volume guard — warn past :data:`STEWARDSHIP_QUEUE_WARN_THRESHOLD` open
       stewardship proposals for the workspace.

    Returns the NEWLY filed Action ids.
    """
    from pocketpaw.fabric.conflicts import detect_open_conflicts
    from pocketpaw.instinct.models import ActionStatus
    from pocketpaw.stores import get_fabric_store, get_instinct_store

    workspace_id = str(workspace_id or "")
    if not workspace_id:
        raise ValueError("sweep_conflicts_to_proposals requires a non-empty workspace_id")

    mode = _source_truth_mode()  # read ONCE per sweep
    if mode not in ("shadow", "enforce"):
        return []

    fabric = fabric_store or get_fabric_store(workspace_id=workspace_id or None)
    conflicts = await detect_open_conflicts(fabric, workspace_id=workspace_id)

    store = get_instinct_store(workspace_id=workspace_id or None)

    # Open (PENDING) stewardship proposals → the one-open-per-key guarantee.
    open_keys: set[tuple[str, str, str]] = set()
    try:
        pending = await store.list_actions(
            pocket_id=workspace_id,
            status=ActionStatus.PENDING,
            workspace_id=workspace_id,
            limit=500,
        )
    except Exception:  # noqa: BLE001 — a failed listing must not crash the sweep
        logger.warning(
            "fabric_conflict: failed to list open proposals for workspace %s — "
            "skipping this sweep rather than risking duplicates",
            workspace_id,
            exc_info=True,
        )
        return []
    for action in pending:
        blob = _conflict_blob(action)
        if blob is not None:
            open_keys.add(_blob_dedupe_key(blob))

    # Reject-memory: (key, signature) pairs a human already dismissed.
    rejected_signatures: set[tuple[tuple[str, str, str], tuple[str, ...]]] = set()
    try:
        rejected = await store.list_actions(
            pocket_id=workspace_id,
            status=ActionStatus.REJECTED,
            workspace_id=workspace_id,
            limit=500,
        )
    except Exception:  # noqa: BLE001 — reject-memory is best-effort
        rejected = []
    for action in rejected:
        blob = _conflict_blob(action)
        if blob is not None:
            signature = tuple(str(s) for s in blob.get("conflict_signature") or [])
            rejected_signatures.add((_blob_dedupe_key(blob), signature))

    filed: list[str] = []
    for conflict in conflicts:
        key = (workspace_id, conflict.object_id, conflict.property)
        if key in open_keys:
            continue  # one open proposal per key — re-sweeps never duplicate
        if (key, tuple(conflict.signature)) in rejected_signatures:
            continue  # the human already kept the policy winner for THIS conflict
        try:
            action_id = await propose_fabric_conflict(
                workspace_id=workspace_id,
                conflict=conflict,
                requested_by=requested_by,
                assignee=assignee,
                fabric_store=fabric,
            )
        except Exception:  # noqa: BLE001 — one bad conflict never blocks the rest
            logger.warning(
                "fabric_conflict: failed to file a proposal for %s.%s (object=%s, ws=%s)",
                conflict.object_type or "object",
                conflict.property,
                conflict.object_id,
                workspace_id,
                exc_info=True,
            )
            continue
        filed.append(action_id)
        open_keys.add(key)

    # Volume guard — the PRD queue-volume metric. ``open_keys`` now holds the
    # pre-existing open proposals plus everything just filed.
    open_count = len(open_keys)
    if open_count > STEWARDSHIP_QUEUE_WARN_THRESHOLD:
        logger.warning(
            "fabric_conflict: stewardship queue for workspace %s has %d open "
            "proposal(s) (threshold %d) — un-rankable conflicts are outpacing "
            "steward review; consider a trust-rule override for the noisy "
            "properties",
            workspace_id,
            open_count,
            STEWARDSHIP_QUEUE_WARN_THRESHOLD,
        )

    if filed:
        logger.info(
            "fabric_conflict: sweep filed %d stewardship proposal(s) for workspace %s",
            len(filed),
            workspace_id,
        )
    return filed


__all__ = [
    "FABRIC_CONFLICT_KIND",
    "FABRIC_CONFLICT_PARAM_KEY",
    "FABRIC_CONFLICT_SCHEMA",
    "STEWARDSHIP_QUEUE_WARN_THRESHOLD",
    "propose_fabric_conflict",
    "sweep_conflicts_to_proposals",
]
