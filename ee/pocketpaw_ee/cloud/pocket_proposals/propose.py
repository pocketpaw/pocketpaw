# ee/cloud/pocket_proposals/propose.py — propose a gated starter-Pocket create.
# Created: 2026-06-19 (SZD-5b — _pocket_create Instinct proposal type).
#
# What this module does (the propose half of the Pocket-create gate): "sovereign
# zero-setup discovery" stages a PROPOSED starter Pocket — a rippleSpec + name (an
# optional template slug) it inferred for a tenant — for a human to review through
# the Instinct gate before any Pocket is created. This files an Instinct ``Action``
# carrying a ``_pocket_create`` blob (schema 1) under ``Action.parameters``. A
# human approves it in The Tray; the apply-on-approve executor
# (``pocket_proposals.executor.execute_approved_pocket_create``) then creates the
# Pocket via the canonical ``pockets.service.create`` write path. This module does
# NOT create a Pocket — it only gates.
#
# The blob is the gated Pocket-create kind, sitting alongside the Fabric-objects
# gate's ``_fabric_objects`` (SZD-5a), the pocket-write bridge's ``_pocket_write``,
# the Belt develop-station's ``_code_change``, the external-action gate's
# ``_external_action``, the mandate foreman's ``_belt_plan``, and the
# Branch-primitive merge gate's ``_artifact_change``. The router + executor
# dispatch on the presence of the ``_pocket_create`` parameters key (the Action
# model has no literal ``kind`` column; the blob also carries
# ``kind="pocket_create"`` for readers that introspect it).
#
# Schema 1 (this is the first version — the schema-version pattern is replicated
# from ``fabric_proposals.propose.FABRIC_OBJECTS_SCHEMA`` /
# ``external_actions.propose.EXTERNAL_ACTION_SCHEMA``, starting at 1). The blob
# carries:
#   * ``schema`` / ``kind`` — version + discriminator;
#   * ``workspace_id`` — the originating tenant. The executor's tenancy gate reads
#     it HERE; a proposed Pocket isn't bound to an EXISTING pocket the way a parked
#     write is, so tenancy lives entirely on the blob — and the new Pocket is
#     created scoped to this workspace. SECURITY: this is a SEPARATE top-level
#     blob field, NOT nested inside ``pocket_spec`` — the correction/edit flow
#     diffs the proposal's editable shape, so keeping tenancy out of the editable
#     spec means a tenant editing the proposal cannot move it to another
#     workspace.
#   * ``user_id`` — the approver/owner the Pocket is created under. Also a SEPARATE
#     top-level blob field for the same reason: the owner cannot be edited via the
#     correction flow (a tenant can't reassign ownership of the staged Pocket).
#   * ``pocket_spec`` — the staged ``CreatePocketRequest`` body the executor
#     model_validates: ``{ripple_spec, name, template_slug?}`` plus any other
#     CreatePocketRequest-shaped fields (description, type, icon, color, ...). This
#     is the ONLY part of the blob that a correction edit may legitimately touch.
#   * ``summary`` — a human-readable one-liner for the gate UI (The Tray);
#   * ``correlation_id`` / ``proposed_event_id`` — the Decision-Graph chain ids,
#     minted here at propose time (``agent.proposed`` opens the chain). The
#     router's approve / reject paths and the executor close the SAME chain.
#
# Security:
#   * NO Pocket is created here — the spec is staged as DATA on the blob and only
#     materialised after a human approves.
#   * Tenancy + owner are bound on SEPARATE top-level blob fields (NOT in the
#     editable ``pocket_spec``) so the correction flow cannot move ownership /
#     workspace. They are bound on the router's approve / reject paths via
#     ``_assert_pocket_create_workspace`` and re-validated at execution (the Pocket
#     is created scoped by the blob's ``workspace_id`` / owned by ``user_id``). A
#     cross-workspace approve OR reject is refused — asymmetric tenant scope is no
#     tenant scope (pocketpaw#1183 / #1250).

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# The Instinct Action kind discriminator for a Pocket-create proposal. The router
# + executor dispatch on the presence of the parameters key below; the blob also
# carries ``kind="pocket_create"`` for readers that introspect it.
POCKET_CREATE_KIND = "pocket_create"

# The parameters key under which the Pocket-create blob rides — peer of the
# Fabric-objects gate's ``_fabric_objects``, the pocket-write bridge's
# ``_pocket_write``, belt's ``_code_change``, and the external-action gate's
# ``_external_action``. The router + executor dispatch on this key being present.
POCKET_CREATE_PARAM_KEY = "_pocket_create"

# Schema version stamped on the ``_pocket_create`` blob. Bump when the blob shape
# changes so a stale pending Action approved after a deploy fails loud instead of
# creating a misinterpreted Pocket (same discipline as the Fabric-objects gate's
# ``FABRIC_OBJECTS_SCHEMA``). Starts at 1 — first version.
POCKET_CREATE_SCHEMA = 1


def _emit_agent_proposed(
    *,
    correlation_id: UUID,
    action_id: str,
    workspace_id: str,
    user_id: str,
    name: str,
) -> UUID | None:
    """Emit the chain-opening ``agent.proposed`` event for a Pocket create.

    Mirrors ``fabric_proposals.propose._emit_agent_proposed``: the proposing
    caller is the actor (``kind="agent"`` with the requesting user on its id, the
    workspace on its scope_context). A proposed Pocket isn't bound to an EXISTING
    pocket — its tenancy is the workspace — so ``pocket_id`` on the chain carries
    the workspace id (matching how the Action's ``pocket_id`` field carries the
    workspace).

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
    intent = f"create the starter Pocket {name!r}"
    payload: dict[str, Any] = {
        # Fields the projection's ``_fold_proposed`` consumes.
        "intent": intent,
        "action": "pocket_create",
        "pocket_id": workspace_id,
        "inputs": [],
        # Richer fields for the explain narrator.
        "proposal_kind": "pocket_create",
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
            "pocket_create agent.proposed emit failed for correlation_id=%s "
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
    """Write ``correlation_id`` + ``proposed_event_id`` onto the persisted
    Action's ``parameters._pocket_create`` blob after ``agent.proposed`` fired.

    The blob is built with ``correlation_id`` already set (minted before build);
    ``proposed_event_id`` is the field this back-write fills in. Direct SQL update
    — the same pattern the Fabric-objects gate's ``_persist_chain_ids`` uses.
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
        blob = params.get(POCKET_CREATE_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["correlation_id"] = correlation_id
        blob["proposed_event_id"] = proposed_event_id
        params[POCKET_CREATE_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — write-back is best-effort
        logger.warning(
            "pocket_create: failed to persist chain ids onto action %s — the "
            "chain's human.corrected will emit without causation_id",
            action_id,
            exc_info=True,
        )


def _normalize_pocket_spec(
    *,
    ripple_spec: dict[str, Any] | None,
    name: str,
    template_slug: str | None,
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the staged ``pocket_spec`` — the editable ``CreatePocketRequest``
    body the executor model_validates.

    Carries ``name`` (required), ``rippleSpec`` (camelCase alias — kept as-is so
    ``CreatePocketRequest.model_validate`` resolves it via the alias), and the
    optional ``templateSlug``. Any ``extra`` keys (description, type, icon, color,
    visibility, ...) are merged in so a richer starter Pocket can be staged — but
    ``workspace_id`` / ``user_id`` are NEVER placed here (they ride as separate
    top-level blob fields so the correction flow can't edit tenancy/owner).

    SECURITY: tenancy keys are stripped from ``extra`` defensively — even if a
    caller passes ``workspace``/``workspace_id``/``owner``/``user_id`` in extra,
    they never reach the editable spec.
    """
    spec: dict[str, Any] = {}
    for k, v in (extra or {}).items():
        # Never let a tenancy/owner key sneak into the editable spec.
        if k in {"workspace", "workspace_id", "owner", "user_id"}:
            continue
        spec[k] = v
    spec["name"] = name
    if ripple_spec is not None:
        # Use the camelCase alias so model_validate resolves it; the DTO sets
        # populate_by_name=True so either spelling works, but the alias is the
        # canonical wire shape.
        spec["rippleSpec"] = ripple_spec
    if template_slug:
        spec["templateSlug"] = template_slug
    return spec


async def propose_pocket(
    *,
    workspace_id: str,
    user_id: str,
    ripple_spec: dict[str, Any] | None = None,
    name: str,
    template_slug: str | None = None,
    extra: dict[str, Any] | None = None,
    summary: str | None = None,
    correlation_id: str | None = None,
    assignee: str | None = None,
) -> str:
    """Build + store an Instinct ``Action`` for a gated starter-Pocket create.

    Files an Action carrying the ``_pocket_create`` blob (schema 1) and opens the
    Decision-Graph chain (``agent.proposed``). Returns the proposed Action id. NO
    Pocket is created here — the spec is staged as DATA and only materialised after
    a human approves.

    Args:
        workspace_id: the originating tenant. Bound on the router's approve /
            reject paths and re-validated at execution; the Pocket is created
            scoped to it. Required — a Pocket create with no tenant is a tenancy
            hole. Rides as a SEPARATE top-level blob field (NOT in ``pocket_spec``)
            so the correction flow can't move it.
        user_id: the approver/owner the Pocket is created under. Required. Also a
            SEPARATE top-level blob field — the owner can't be edited via the
            correction flow.
        ripple_spec: the staged rippleSpec for the new Pocket. Optional — a
            template-only proposal can omit it (the template compiles into the spec
            at create time). Stored under the ``rippleSpec`` camelCase alias inside
            ``pocket_spec``.
        name: the new Pocket's name. Required + non-empty.
        template_slug: an optional bundled-template slug to compile-on-install.
        extra: optional additional ``CreatePocketRequest`` fields (description,
            type, icon, color, visibility, ...) merged into the staged spec.
            Tenancy/owner keys are stripped defensively.
        summary: a human-readable one-liner for the gate UI. Defaults to a sensible
            one built from the name.
        correlation_id: an optional pre-minted chain id. When omitted a fresh one
            is minted here (the common case).
        assignee: the workspace member who should approve. Defaults to ``user_id``
            so the proposer's queue carries it.
    """
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_instinct_store

    workspace_id = str(workspace_id or "")
    if not workspace_id:
        raise ValueError("propose_pocket requires a non-empty workspace_id")
    user_id = str(user_id or "")
    if not user_id:
        raise ValueError("propose_pocket requires a non-empty user_id (the owner)")
    name = str(name or "").strip()
    if not name:
        raise ValueError("propose_pocket requires a non-empty name")

    pocket_spec = _normalize_pocket_spec(
        ripple_spec=ripple_spec,
        name=name,
        template_slug=template_slug,
        extra=extra,
    )

    # Mint the chain correlation_id BEFORE building the blob so the stored Action
    # carries it from the first write. The same id threads through approve / reject
    # (router) and execute (executor) so the whole create folds into ONE Decision
    # chain.
    corr = correlation_id or str(uuid4())

    human_summary = summary or f"Create the starter Pocket {name!r}."

    blob: dict[str, Any] = {
        "kind": POCKET_CREATE_KIND,
        "schema": POCKET_CREATE_SCHEMA,
        # Tenancy + owner are SEPARATE top-level fields (NOT in pocket_spec) so the
        # correction/edit flow can never change them.
        "workspace_id": workspace_id,
        "user_id": user_id,
        # The editable staged CreatePocketRequest body.
        "pocket_spec": pocket_spec,
        "summary": human_summary,
        # RFC 09 chain-correlation fields (schema 1 carries them from the start).
        "correlation_id": corr,
        "proposed_event_id": None,
    }

    title = f"Starter Pocket — {name}"
    recommendation = f"Approve to create the proposed starter Pocket. {human_summary}"
    trigger = ActionTrigger(
        type="agent",
        source=user_id or "pocket_create",
        reason="proposed starter Pocket requires approval",
    )

    # ISO: scope the store to the caller's workspace (validated non-empty above)
    # — this propose path has no ``current_workspace`` ContextVar set.
    store = get_instinct_store(workspace_id=workspace_id or None)
    action_obj = await store.propose(
        # ``pocket_id`` carries the workspace for Pocket-create proposals — they
        # aren't bound to an EXISTING pocket the way Mission Control items are
        # (mirrors the Fabric-objects gate). The workspace also rides on the blob
        # (the executor's tenancy gate reads it there); pocket_id mirrors it so the
        # existing per-pocket queries still surface the row.
        pocket_id=workspace_id,
        title=title,
        description=recommendation,
        recommendation=recommendation,
        trigger=trigger,
        category=ActionCategory.WORKFLOW,
        priority=ActionPriority.MEDIUM,
        parameters={POCKET_CREATE_PARAM_KEY: blob},
        assignee=assignee or user_id or None,
        workspace_id=workspace_id,
    )

    logger.info(
        "pocket_create: proposed starter Pocket %r → Instinct action %s "
        "(workspace=%s, owner=%s, correlation_id=%s)",
        name,
        action_obj.id,
        workspace_id,
        user_id,
        corr,
    )

    # Open the Decision-Graph chain now that the Action is stored. ``agent.
    # proposed`` is the chain origin; its event id is back-written onto the blob so
    # the router's ``human.corrected`` can cite it as causation. Best-effort: a
    # Decision-Graph wiring failure must NOT fail the propose response.
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
    "POCKET_CREATE_KIND",
    "POCKET_CREATE_PARAM_KEY",
    "POCKET_CREATE_SCHEMA",
    "propose_pocket",
]
