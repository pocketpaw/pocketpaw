# ee/pocketpaw_ee/cloud/ship/propose.py — file a gated /ship infrastructure
# action through Instinct (the human approve/reject layer, "sudo for agents").
#
# The propose half of the /ship gate. ``destroy`` (a box or an app), ``rollback``,
# and a deploy to a PROD-flagged app never execute from a tool call or an HTTP
# request: they file an Instinct ``Action`` carrying a ``_ship_action`` blob and
# return a proposal id. A human approves in The Tray, and only then does
# ``ship.executor.execute_approved_ship_action`` touch the box.
#
# This is the SIXTH gated proposal kind, alongside ``_pocket_write``,
# ``_code_change`` (belt), ``_external_action``, ``_admin_action`` and
# ``_artifact_change``. The blob shape and discipline deliberately mirror
# ``external_actions.propose`` — same params-hash re-validation at execute, same
# idempotency key, same Decision-Graph chain fields — because the approve-path
# dispatch in ``instinct/router.py`` treats all the kinds uniformly.
#
# SECURITY: no SSH key material, no connection string, and no env VALUE is ever
# written to the blob. It carries ids (workspace, box, app) and the verb; the
# executor resolves the box's credential fresh through ``ship.engine.box_session``
# (which decrypts to a 0600 temp file and shreds it) at execution time.
#
# Created 2026-07-22 (feat/ship-4-agent-surface, SHIP-4): new module.
# Updated: 2026-10-01 (CN-5) — the chain emit + chain-id back-write now go through
#   the shared ``cloud/_core/proposals`` helper (store API, no raw SQL).

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from pocketpaw_ee.cloud._core.proposals import emit_agent_proposed, persist_chain_ids

logger = logging.getLogger(__name__)

# The key the blob lives under on ``Action.parameters``.
SHIP_ACTION_PARAM_KEY = "_ship_action"
# Discriminator + schema version. Bump the schema when the blob shape changes so
# a stale pending Action approved after a deploy fails LOUD rather than firing a
# misinterpreted infrastructure verb.
SHIP_ACTION_KIND = "ship_action"
SHIP_ACTION_SCHEMA = 1

# The verbs that may NEVER execute without an approval. ``deploy_app`` is gated
# conditionally (only for a prod-flagged app) and so is not in this set — the
# caller decides and passes ``verb="deploy_app"`` only when it is gated.
GATED_VERBS = frozenset({"destroy_box", "destroy_app", "rollback", "deploy_app"})


def compute_params_hash(verb: str, params: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex digest of ``verb`` + ``params``.

    Canonical JSON (sorted keys, no whitespace) so the same logical action hashes
    identically regardless of dict ordering. The executor recomputes this off the
    persisted blob and REFUSES the action when it no longer matches — a human
    approved a SPECIFIC teardown, and an edit between propose and approve must
    never silently destroy something else.
    """
    canonical = json.dumps(
        {"verb": verb, "params": params or {}},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def propose_ship_action(
    *,
    workspace_id: str,
    verb: str,
    box_id: str = "",
    app_id: str = "",
    target_label: str = "",
    params: dict[str, Any] | None = None,
    requested_by: str,
    idempotency_key: str | None = None,
    summary: str | None = None,
    correlation_id: str | None = None,
    assignee: str | None = None,
) -> str:
    """Build + store an Instinct ``Action`` for a gated /ship verb.

    Returns the proposed Action id — the ``proposal_id`` the caller hands back to
    the agent or the HTTP client. NOTHING is executed here.

    Args:
        workspace_id: the originating tenant. The executor's tenancy gate reads
            it off the blob and refuses an empty one.
        verb: one of ``GATED_VERBS``.
        box_id / app_id: the target ids. ``destroy_box`` needs a box; the app
            verbs need an app (and carry its box for the session).
        target_label: a human label for the gate UI ("box paw-ship-abc",
            "app demo"). Falls back to the ids.
        params: verb params (e.g. the rollback target image). Hashed into
            ``params_hash`` so a post-propose edit is refused at execute.
        requested_by: the user id that proposed (chain actor + audit + the
            authorization re-check at execute).
        idempotency_key: so the executor never double-fires. Defaults to a
            deterministic value keyed on the target + params hash, so an
            identical re-propose dedupes naturally.
        summary: one-liner for the gate UI; a sensible default is built.
        correlation_id: an optional pre-minted chain id (fresh one when omitted).
        assignee: who should approve. Defaults to the proposer.
    """
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_instinct_store

    workspace_id = str(workspace_id or "")
    if not workspace_id:
        raise ValueError("propose_ship_action requires a non-empty workspace_id")
    verb = str(verb or "")
    if verb not in GATED_VERBS:
        raise ValueError(f"propose_ship_action got a non-gated verb: {verb!r}")
    if not box_id and not app_id:
        raise ValueError("propose_ship_action requires a box_id or an app_id")

    call_params = dict(params or {})
    params_hash = compute_params_hash(verb, call_params)
    label = target_label or (f"app {app_id}" if app_id else f"box {box_id}")
    idem = idempotency_key or f"{workspace_id}:{verb}:{app_id or box_id}:{params_hash[:16]}"
    corr = correlation_id or str(uuid4())
    human_summary = summary or f"{verb.replace('_', ' ').capitalize()} — {label}."

    blob: dict[str, Any] = {
        "kind": SHIP_ACTION_KIND,
        "schema": SHIP_ACTION_SCHEMA,
        "workspace_id": workspace_id,
        "verb": verb,
        "box_id": box_id,
        "app_id": app_id,
        "target_label": label,
        "params": call_params,
        "params_hash": params_hash,
        "idempotency_key": idem,
        "requested_by": requested_by,
        "summary": human_summary,
        "correlation_id": corr,
        "proposed_event_id": None,
        # Back-written by the executor: {status, detail, executed_at}.
        "outcome": None,
    }

    title = f"Infrastructure — {verb.replace('_', ' ')} {label}"
    recommendation = (
        f"Approve to {verb.replace('_', ' ')} {label}. This is irreversible "
        f"infrastructure work on a live box. {human_summary}"
    )
    trigger = ActionTrigger(
        type="agent",
        source=requested_by or "ship",
        reason=f"/ship '{verb}' on {label} requires approval",
    )

    # Scope the store to the caller's workspace — this propose path has no
    # ``current_workspace`` ContextVar set (mirrors the external-action helper).
    store = get_instinct_store(workspace_id=workspace_id or None)
    action_obj = await store.propose(
        # ``pocket_id`` carries the workspace: a /ship action is tenant-scoped,
        # not pocket-bound (same as external actions + belt).
        pocket_id=workspace_id,
        title=title,
        description=recommendation,
        recommendation=recommendation,
        trigger=trigger,
        category=ActionCategory.EXTERNAL,
        priority=ActionPriority.HIGH,
        parameters={SHIP_ACTION_PARAM_KEY: blob},
        assignee=assignee or requested_by or None,
        workspace_id=workspace_id,
    )

    action_id = str(getattr(action_obj, "id", "") or "")
    proposed_event_id = emit_agent_proposed(
        correlation_id=UUID(corr),
        action_id=action_id,
        kind="ship_action",
        intent=f"{verb.replace('_', ' ')} on {label}",
        proposal={"verb": verb, "target": label},
        workspace_id=workspace_id,
        user_id=requested_by,
    )
    if proposed_event_id is not None:
        await persist_chain_ids(
            store=store,
            action_id=action_id,
            param_key=SHIP_ACTION_PARAM_KEY,
            correlation_id=corr,
            proposed_event_id=str(proposed_event_id),
        )

    logger.info(
        "ship: proposed '%s' on %s → Instinct action %s (workspace=%s)",
        verb,
        label,
        action_id,
        workspace_id,
    )
    return action_id
