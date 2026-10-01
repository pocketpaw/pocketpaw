# ee/cloud/admin_proposals/propose.py — propose a gated workspace-admin action.
# Created: 2026-07-03 (feat/workspace-admin-tools, WA-2).
# Updated: 2026-10-01 (CN-5) — the chain emit + chain-id back-write now go through
#   the shared ``cloud/_core/proposals`` helper (store API, no raw SQL).
#
# What this module does (the propose half of the admin-action gate): a
# workspace-admin tool (e.g. ``member_update_role`` in the workspace_admin MCP
# server) proposes a WRITE to a workspace-admin service — "run RBAC action
# ``workspace.member.role_change`` with args ``{...}``". This files an Instinct
# ``Action`` carrying an ``_admin_action`` blob (schema 1) under
# ``Action.parameters``. A human approves it in The Tray; the apply-on-approve
# executor (``admin_proposals.executor.execute_approved_admin_action``) then
# fires the whitelisted service call — AFTER re-checking the proposer's CURRENT
# RBAC role. This module does NOT mutate — it only gates.
#
# The blob is the 8th gated proposal kind, sitting alongside the external-action
# ``_external_action`` and the pocket-write ``_pocket_write``. The router +
# executor dispatch on the presence of the ``_admin_action`` parameters key. The
# blob shape (minimal + typed):
#   * ``schema`` / ``kind`` — version + discriminator;
#   * ``workspace_id`` — the originating tenant (the executor's tenancy gate +
#     RBAC re-check are scoped to it; the router's tenancy gate reads it HERE);
#   * ``action`` — the RBAC action KEY the executor whitelists + re-checks (e.g.
#     ``"workspace.member.role_change"``). An unknown/absent key → hard fail;
#   * ``args`` — the service-call args (DATA — passed to the whitelisted service
#     adapter; never interpolated into a shell);
#   * ``proposer_user_id`` — the user who proposed the write. The executor loads
#     THIS user fresh at approve time and RE-CHECKS their CURRENT workspace role
#     (a demoted proposer's approved action fails closed);
#   * ``params_hash`` — a stable hash of ``action`` + ``args`` so the executor
#     refuses if the args were tampered with between propose and approve (the
#     human approved a SPECIFIC write);
#   * ``idempotency_key`` — so the executor never double-fires on re-invocation
#     (bulk re-approve, retry);
#   * ``correlation_id`` / ``proposed_event_id`` — the Decision-Graph chain ids.
#   * ``summary`` — a human-readable one-liner for the gate UI (The Tray).
#
# Security:
#   * NO privilege is granted by proposing — the executor RE-RESOLVES the
#     proposer's CURRENT role and re-checks the RBAC action before firing.
#   * The args hash is re-checked at execution — an args edit between propose and
#     approve refuses the write.
#   * Tenancy is bound on the router's approve / reject paths via
#     ``_assert_admin_action_workspace`` and re-validated at execution.

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from pocketpaw_ee.cloud._core.proposals import emit_agent_proposed, persist_chain_ids

logger = logging.getLogger(__name__)

# The Instinct Action kind discriminator for an admin-action proposal. The
# blob also carries ``kind="admin_action"`` for readers that introspect it.
ADMIN_ACTION_KIND = "admin_action"

# The parameters key under which the admin-action blob rides — peer of the
# external-action bridge's ``_external_action``. The router + executor dispatch
# on this key being present.
ADMIN_ACTION_PARAM_KEY = "_admin_action"

# Schema version stamped on the ``_admin_action`` blob. Bump when the blob shape
# changes so a stale pending Action approved after a deploy fails loud instead of
# firing a misinterpreted admin write (same discipline as the external-action
# ``EXTERNAL_ACTION_SCHEMA``). Starts at 1 — this is the first version.
ADMIN_ACTION_SCHEMA = 1


def compute_args_hash(action: str, args: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex digest of the RBAC ``action`` + ``args``.

    Canonical JSON (sorted keys, no whitespace) so the same logical write hashes
    identically regardless of dict ordering. The executor recomputes this off the
    persisted blob and refuses the write if it no longer matches — a human
    approved a SPECIFIC write, and an args edit between propose and approve must
    not silently fire a different one.
    """
    canonical = json.dumps(
        {"action": action, "args": args or {}},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def propose_admin_action(
    *,
    workspace_id: str,
    action: str,
    args: dict[str, Any] | None = None,
    proposer_user_id: str,
    idempotency_key: str | None = None,
    summary: str | None = None,
    title: str | None = None,
    correlation_id: str | None = None,
    assignee: str | None = None,
) -> str:
    """Build + store an Instinct ``Action`` for a gated workspace-admin write.

    Files an Action carrying the ``_admin_action`` blob (schema 1) and opens the
    Decision-Graph chain (``agent.proposed``). Returns the proposed Action id.
    Proposing grants NO privilege — the executor re-resolves the proposer's
    CURRENT role and re-checks the RBAC action before firing on approval.

    Args:
        workspace_id: the originating tenant. Bound on the router's approve /
            reject paths and re-validated at execution. Required.
        action: the RBAC action KEY the executor whitelists + re-checks (e.g.
            ``"workspace.member.role_change"``).
        args: the service-call args (e.g. ``{"target_user_id": "...",
            "role": "admin"}``). Hashed into ``params_hash`` so the executor can
            refuse a post-propose args edit.
        proposer_user_id: the user id that proposed the write. The executor loads
            THIS user fresh at approve time and re-checks their CURRENT role.
        idempotency_key: caller-supplied key so the executor never double-fires.
            Defaults to a deterministic value derived from the args hash.
        summary / title: human-readable strings for the gate UI (defaults built
            from the action when omitted).
        correlation_id: an optional pre-minted chain id (a fresh one is minted
            when omitted — the common case).
        assignee: the workspace member who should approve. Defaults to
            ``proposer_user_id``.
    """
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_instinct_store

    workspace_id = str(workspace_id or "")
    if not workspace_id:
        raise ValueError("propose_admin_action requires a non-empty workspace_id")
    action = str(action or "")
    if not action:
        raise ValueError("propose_admin_action requires a non-empty action")
    proposer_user_id = str(proposer_user_id or "")
    if not proposer_user_id:
        raise ValueError("propose_admin_action requires a non-empty proposer_user_id")

    call_args = dict(args or {})
    args_hash = compute_args_hash(action, call_args)
    # Deterministic idempotency key when the caller doesn't supply one — keyed on
    # workspace + action + args hash. NOTE: dedup is per-Action, not cross-Action:
    # the executor's guard stops a SINGLE approved Action from firing twice (via
    # its recorded outcome/status), but two distinct proposals with the same key
    # each execute independently. The key is stored for future cross-Action dedup
    # and for tracing; it does not itself prevent a re-propose from running.
    idem = idempotency_key or f"{workspace_id}:{action}:{args_hash[:16]}"

    corr = correlation_id or str(uuid4())
    human_summary = summary or f"Run admin action '{action}' (scope: workspace)."
    human_title = title or f"Workspace admin — {action}"

    blob: dict[str, Any] = {
        "kind": ADMIN_ACTION_KIND,
        "schema": ADMIN_ACTION_SCHEMA,
        "workspace_id": workspace_id,
        "action": action,
        "args": call_args,
        "proposer_user_id": proposer_user_id,
        "params_hash": args_hash,
        "idempotency_key": idem,
        "summary": human_summary,
        # Decision-Graph chain-correlation fields (schema 1 carries them).
        "correlation_id": corr,
        "proposed_event_id": None,
    }

    recommendation = f"Approve to run admin action '{action}'. {human_summary}"
    trigger = ActionTrigger(
        type="agent",
        source=proposer_user_id or "admin_action",
        reason=f"workspace-admin action '{action}' requires human approval",
    )

    # Scope the store to the caller's workspace (validated non-empty above) so the
    # proposal lands in the tenant's file — this propose path has no
    # ``current_workspace`` ContextVar set.
    store = get_instinct_store(workspace_id=workspace_id or None)
    action_obj = await store.propose(
        # ``pocket_id`` carries the workspace for admin actions — they aren't
        # bound to a pocket (mirrors external_actions). The workspace also rides
        # on the blob (the executor's tenancy gate + RBAC re-check read it there).
        pocket_id=workspace_id,
        title=human_title,
        description=recommendation,
        recommendation=recommendation,
        trigger=trigger,
        category=ActionCategory.WORKFLOW,
        priority=ActionPriority.HIGH,
        parameters={ADMIN_ACTION_PARAM_KEY: blob},
        assignee=assignee or proposer_user_id or None,
        workspace_id=workspace_id,
    )

    logger.info(
        "admin_action: proposed action '%s' → Instinct action %s "
        "(workspace=%s, proposer=%s, correlation_id=%s)",
        action,
        action_obj.id,
        workspace_id,
        proposer_user_id,
        corr,
    )

    # Open the Decision-Graph chain now that the Action is stored. Best-effort: a
    # wiring failure must NOT fail the propose response.
    proposed_event_id = emit_agent_proposed(
        correlation_id=UUID(corr),
        action_id=action_obj.id,
        kind="admin_action",
        intent=f"workspace-admin action '{action}'",
        proposal={"rbac_action": action},
        workspace_id=workspace_id,
        user_id=proposer_user_id,
    )
    if proposed_event_id is not None:
        await persist_chain_ids(
            store=store,
            action_id=action_obj.id,
            param_key=ADMIN_ACTION_PARAM_KEY,
            correlation_id=corr,
            proposed_event_id=str(proposed_event_id),
        )

    return action_obj.id


__all__ = [
    "ADMIN_ACTION_KIND",
    "ADMIN_ACTION_PARAM_KEY",
    "ADMIN_ACTION_SCHEMA",
    "compute_args_hash",
    "propose_admin_action",
]
