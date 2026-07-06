# ee/cloud/instinct_rule_proposals/propose.py — propose a gated governed-rule create.
# Created: 2026-06-20 (S2-R3 — _instinct_rule Instinct proposal type).
#
# What this module does (the propose half of the governed-rule gate): "sovereign
# zero-setup discovery" reverse-engineers a candidate governed rule (a CEL ``when``
# + a gate ``action``, scoped to a tenant) from a workspace's exhaust — audit rows,
# corrections, the on-box ontology — and stages it as a PROPOSED rule for a human to
# approve / edit / reject through the Instinct gate before any rule becomes active.
# This files an Instinct ``Action`` carrying an ``_instinct_rule`` blob (schema 1)
# under ``Action.parameters``. A human approves it in The Tray; the apply-on-approve
# executor (``instinct_rule_proposals.executor.execute_approved_instinct_rule``) then
# persists the rule via the canonical ``rules.service.create_rule`` write path. This
# module does NOT create a rule — it only gates.
#
# The blob is the gated governed-rule kind, sitting alongside the Pocket-create
# gate's ``_pocket_create`` (SZD-5b), the Fabric-objects gate's ``_fabric_objects``
# (SZD-5a), the pocket-write bridge's ``_pocket_write``, the Belt develop-station's
# ``_code_change``, the external-action gate's ``_external_action``, the mandate
# foreman's ``_belt_plan``, and the Branch-primitive merge gate's ``_artifact_change``.
# The router + executor dispatch on the presence of the ``_instinct_rule`` parameters
# key (the Action model has no literal ``kind`` column; the blob also carries
# ``kind="instinct_rule"`` for readers that introspect it).
#
# Schema 1 (first version — the schema-version pattern is replicated from
# ``pocket_proposals.propose.POCKET_CREATE_SCHEMA``). The blob carries:
#   * ``schema`` / ``kind`` — version + discriminator;
#   * ``workspace_id`` — the originating tenant. The executor's tenancy gate reads it
#     HERE; a proposed rule isn't bound to an EXISTING pocket, so tenancy lives on the
#     blob — and the rule is created scoped to this workspace. SECURITY: this is a
#     SEPARATE top-level blob field, NOT nested inside ``rule_spec`` — the
#     correction/edit flow diffs the proposal's editable shape, so keeping tenancy out
#     of the editable spec means a tenant editing the proposal cannot move it to
#     another workspace.
#   * ``user_id`` — the approver/owner the rule is owned by. Also a SEPARATE top-level
#     blob field for the same reason: the owner cannot be edited via the correction
#     flow (a tenant can't reassign ownership of the staged rule).
#   * ``rule_spec`` — the editable ``RuleDraft``-shaped sub-dict the executor
#     model_validates: ``{name, description?, when (CEL), action, scope, confidence,
#     provenance}``. This is the ONLY part of the blob a correction edit may touch.
#   * ``summary`` — a human-readable one-liner for the gate UI (The Tray);
#   * ``correlation_id`` / ``proposed_event_id`` — the Decision-Graph chain ids,
#     minted here at propose time (``agent.proposed`` opens the chain). The router's
#     approve / reject paths and the executor close the SAME chain.
#
# Security:
#   * NO rule is created here — the spec is staged as DATA on the blob and only
#     persisted after a human approves.
#   * Tenancy + owner are bound on SEPARATE top-level blob fields (NOT in the editable
#     ``rule_spec``) so the correction flow cannot move ownership / workspace. The
#     router's (S2-R4) ``_assert_instinct_rule_workspace`` is the primary tenancy
#     gate on the FOUR approve/reject paths; the executor re-validates here. A
#     cross-workspace approve OR reject is refused — asymmetric tenant scope is no
#     tenant scope (pocketpaw#1183 / #1250). The R3 blob keeps ``workspace_id`` as the
#     tenancy anchor so R4 can assert on it.

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# The Instinct Action kind discriminator for a governed-rule proposal. The router
# + executor dispatch on the presence of the parameters key below; the blob also
# carries ``kind="instinct_rule"`` for readers that introspect it.
INSTINCT_RULE_KIND = "instinct_rule"

# The parameters key under which the governed-rule blob rides — peer of the
# Pocket-create gate's ``_pocket_create``, the Fabric-objects gate's
# ``_fabric_objects``, belt's ``_code_change``, and the external-action gate's
# ``_external_action``. The router + executor dispatch on this key being present.
INSTINCT_RULE_PARAM_KEY = "_instinct_rule"

# Schema version stamped on the ``_instinct_rule`` blob. Bump when the blob shape
# changes so a stale pending Action approved after a deploy fails loud instead of
# persisting a misinterpreted rule (same discipline as the Pocket-create gate's
# ``POCKET_CREATE_SCHEMA``). Starts at 1 — first version.
INSTINCT_RULE_SCHEMA = 1


def _emit_agent_proposed(
    *,
    correlation_id: UUID,
    action_id: str,
    workspace_id: str,
    user_id: str,
    name: str,
) -> UUID | None:
    """Emit the chain-opening ``agent.proposed`` event for a governed-rule create.

    Mirrors ``pocket_proposals.propose._emit_agent_proposed``: the proposing caller
    is the actor (``kind="agent"`` with the requesting user on its id, the workspace
    on its scope_context). A proposed rule isn't bound to an EXISTING pocket — its
    tenancy is the workspace — so ``pocket_id`` on the chain carries the workspace id
    (matching how the Action's ``pocket_id`` field carries the workspace).

    Returns the emitted event id so the caller can persist it on the blob's
    ``proposed_event_id`` field for the ``human.corrected`` causation chain, or
    ``None`` when the emit raised — best-effort per RFC 09; the Slice 4 reconciler
    picks up any orphans.
    """
    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_agent_proposed

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}"],
    )
    intent = f"create the governed rule {name!r}"
    payload: dict[str, Any] = {
        # Fields the projection's ``_fold_proposed`` consumes.
        "intent": intent,
        "action": "instinct_rule",
        "pocket_id": workspace_id,
        "inputs": [],
        # Richer fields for the explain narrator.
        "proposal_kind": "instinct_rule",
        "proposal": {"name": name},
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
            "instinct_rule agent.proposed emit failed for correlation_id=%s "
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
    """Write ``correlation_id`` + ``proposed_event_id`` onto the persisted Action's
    ``parameters._instinct_rule`` blob after ``agent.proposed`` fired.

    The blob is built with ``correlation_id`` already set (minted before build);
    ``proposed_event_id`` is the field this back-write fills in. Direct SQL update —
    the same pattern the Pocket-create gate's ``_persist_chain_ids`` uses.
    Best-effort: a write failure leaves ``proposed_event_id`` None and the eventual
    ``human.corrected`` emits without a causation_id (the chain still folds;
    causation_id is optional on EventEntry).
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(INSTINCT_RULE_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["correlation_id"] = correlation_id
        blob["proposed_event_id"] = proposed_event_id
        params[INSTINCT_RULE_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — write-back is best-effort
        logger.warning(
            "instinct_rule: failed to persist chain ids onto action %s — the "
            "chain's human.corrected will emit without causation_id",
            action_id,
            exc_info=True,
        )


def _rule_name(rule_spec: dict[str, Any]) -> str:
    """Read a human-readable name off the rule_spec for titles / summaries."""
    name = str(rule_spec.get("name") or "").strip()
    return name or "governed rule"


async def propose_instinct_rule(
    *,
    workspace_id: str,
    user_id: str,
    rule_spec: dict[str, Any],
    summary: str | None = None,
    correlation_id: str | None = None,
    assignee: str | None = None,
) -> str:
    """Build + store an Instinct ``Action`` for a gated governed-rule create.

    Files an Action carrying the ``_instinct_rule`` blob (schema 1) and opens the
    Decision-Graph chain (``agent.proposed``). Returns the proposed Action id. NO
    rule is created here — the ``rule_spec`` is staged as DATA and only persisted
    after a human approves.

    Args:
        workspace_id: the originating tenant. Bound on the router's approve / reject
            paths (S2-R4) and re-validated at execution; the rule is created scoped
            to it. Required — a rule create with no tenant is a tenancy hole. Rides
            as a SEPARATE top-level blob field (NOT in ``rule_spec``) so the
            correction flow can't move it.
        user_id: the approver/owner the rule is owned by. Required. Also a SEPARATE
            top-level blob field — the owner can't be edited via the correction flow.
        rule_spec: the editable ``RuleDraft``-shaped sub-dict (``{name, description?,
            when, action, scope, confidence, provenance}``). Staged verbatim; the
            executor ``RuleDraft.model_validate``s it at the gate chokepoint. The ONLY
            tenant-editable part of the blob.
        summary: a human-readable one-liner for the gate UI. Defaults to a sensible
            one built from the rule name.
        correlation_id: an optional pre-minted chain id. When omitted a fresh one is
            minted here (the common case).
        assignee: the workspace member who should approve. Defaults to ``user_id`` so
            the proposer's queue carries it.
    """
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_instinct_store

    workspace_id = str(workspace_id or "")
    if not workspace_id:
        raise ValueError("propose_instinct_rule requires a non-empty workspace_id")
    user_id = str(user_id or "")
    if not user_id:
        raise ValueError("propose_instinct_rule requires a non-empty user_id (the owner)")
    if not isinstance(rule_spec, dict) or not rule_spec:
        raise ValueError("propose_instinct_rule requires a non-empty rule_spec")

    name = _rule_name(rule_spec)

    # Mint the chain correlation_id BEFORE building the blob so the stored Action
    # carries it from the first write. The same id threads through approve / reject
    # (router) and execute (executor) so the whole create folds into ONE Decision
    # chain.
    corr = correlation_id or str(uuid4())

    human_summary = summary or f"Create the governed rule {name!r}."

    blob: dict[str, Any] = {
        "kind": INSTINCT_RULE_KIND,
        "schema": INSTINCT_RULE_SCHEMA,
        # Tenancy + owner are SEPARATE top-level fields (NOT in rule_spec) so the
        # correction/edit flow can never change them.
        "workspace_id": workspace_id,
        "user_id": user_id,
        # The editable staged RuleDraft body.
        "rule_spec": rule_spec,
        "summary": human_summary,
        # RFC 09 chain-correlation fields (schema 1 carries them from the start).
        "correlation_id": corr,
        "proposed_event_id": None,
    }

    title = f"Governed rule — {name}"
    recommendation = f"Approve to create the proposed governed rule. {human_summary}"
    trigger = ActionTrigger(
        type="agent",
        source=user_id or "instinct_rule",
        reason="proposed governed rule requires approval",
    )

    store = get_instinct_store()
    action_obj = await store.propose(
        # ``pocket_id`` carries the workspace for rule-create proposals — they aren't
        # bound to an EXISTING pocket the way Mission Control items are (mirrors the
        # Pocket-create gate). The workspace also rides on the blob (the executor's
        # tenancy gate reads it there); pocket_id mirrors it so the existing
        # per-pocket queries still surface the row.
        pocket_id=workspace_id,
        title=title,
        description=recommendation,
        recommendation=recommendation,
        trigger=trigger,
        category=ActionCategory.WORKFLOW,
        priority=ActionPriority.MEDIUM,
        parameters={INSTINCT_RULE_PARAM_KEY: blob},
        assignee=assignee or user_id or None,
        workspace_id=workspace_id,
    )

    logger.info(
        "instinct_rule: proposed governed rule %r → Instinct action %s "
        "(workspace=%s, owner=%s, correlation_id=%s)",
        name,
        action_obj.id,
        workspace_id,
        user_id,
        corr,
    )

    # Open the Decision-Graph chain now that the Action is stored. ``agent.proposed``
    # is the chain origin; its event id is back-written onto the blob so the router's
    # ``human.corrected`` can cite it as causation. Best-effort: a Decision-Graph
    # wiring failure must NOT fail the propose response.
    proposed_event_id = _emit_agent_proposed(
        correlation_id=UUID(corr),
        action_id=action_obj.id,
        workspace_id=workspace_id,
        user_id=user_id,
        name=name,
    )
    if proposed_event_id is not None:
        await _persist_chain_ids(
            store=store,
            action_id=action_obj.id,
            correlation_id=corr,
            proposed_event_id=str(proposed_event_id),
        )

    return action_obj.id


__all__ = [
    "INSTINCT_RULE_KIND",
    "INSTINCT_RULE_PARAM_KEY",
    "INSTINCT_RULE_SCHEMA",
    "propose_instinct_rule",
]
