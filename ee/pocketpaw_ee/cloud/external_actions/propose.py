# ee/cloud/external_actions/propose.py — propose a gated external-action call.
# Created: 2026-06-11 (feat/external-action-proposal).
# Updated: 2026-10-01 (CN-5) — the chain emit + chain-id back-write now go through
#   the shared ``cloud/_core/proposals`` helper (store API, no raw SQL).
#
# What this module does (the propose half of the external-action gate): an
# agent (or any caller) proposes a call to an external system through a bound
# connector — "run action ``approveApplication`` on connector ``crm`` with
# params ``{...}``". This files an Instinct ``Action`` carrying an
# ``_external_action`` blob (schema 1) under ``Action.parameters``. A human
# approves it in The Tray; the apply-on-approve executor
# (``external_actions.executor.execute_approved_external_action``) then makes
# the connector call. This module does NOT call the connector — it only gates.
#
# The blob is the third gated proposal kind, sitting alongside the pocket-write
# bridge's ``_pocket_write`` and the Belt develop-station's ``_code_change``.
# The router + executor dispatch on the presence of the ``_external_action``
# parameters key (the Action model has no literal ``kind`` column; the blob
# also carries ``kind="external_action"`` for readers that introspect it).
#
# Schema 1 (this is the first version — the schema-version pattern is replicated
# from ``instinct_bridge._POCKET_WRITE_SCHEMA`` / belt's ``CODE_CHANGE_SCHEMA``,
# starting at 1). The blob carries:
#   * ``schema`` / ``kind`` — version + discriminator;
#   * ``workspace_id`` — the originating tenant (the executor's tenancy gate
#     reads it HERE; an external action isn't bound to a pocket the way a parked
#     write is, so tenancy lives entirely on the blob);
#   * ``connector_name`` / ``scope`` / ``pocket_id`` — the connector reference
#     (which bound connector, at which scope) the executor resolves;
#   * ``action`` — the named connector action to call;
#   * ``params`` — the proposed call params (DATA — never interpolated into a
#     shell; passed verbatim to the connector adapter);
#   * ``params_hash`` — a stable hash of ``action`` + ``params`` so the executor
#     can refuse if the params were tampered with between propose and approve;
#   * ``idempotency_key`` — so the executor never double-fires the HTTP call if
#     re-invoked (bulk re-approve, retry, etc.);
#   * ``correlation_id`` / ``proposed_event_id`` — the Decision-Graph chain ids,
#     minted here at propose time (``agent.proposed`` opens the chain). The
#     router's approve / reject paths and the executor close the SAME chain.
#   * ``summary`` — a human-readable one-liner for the gate UI (The Tray).
#
# Security:
#   * NO connector secret reaches the Instinct DB. The blob carries the
#     connector NAME + scope only; the credential is resolved fresh at execution
#     by the cloud connector service from the workspace's saved config.
#   * The params hash is re-checked at execution — a params edit between propose
#     and approve refuses the call (the human approved a specific call, not an
#     arbitrary one).
#   * Tenancy is bound on the router's approve / reject paths via
#     ``_assert_external_action_workspace`` and re-validated at execution (the
#     connector service is scoped by the blob's ``workspace_id``).

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from pocketpaw_ee.cloud._core.proposals import emit_agent_proposed, persist_chain_ids

logger = logging.getLogger(__name__)

# The Instinct Action kind discriminator for an external-action proposal. The
# router + executor dispatch on the presence of the parameters key below; the
# blob also carries ``kind="external_action"`` for readers that introspect it.
EXTERNAL_ACTION_KIND = "external_action"

# The parameters key under which the external-action blob rides — peer of the
# pocket-write bridge's ``_pocket_write`` and belt's ``_code_change``. The
# router + executor dispatch on this key being present.
EXTERNAL_ACTION_PARAM_KEY = "_external_action"

# Schema version stamped on the ``_external_action`` blob. Bump when the blob
# shape changes so a stale pending Action approved after a deploy fails loud
# instead of firing a misinterpreted external call (same discipline as the
# pocket-write bridge's ``_POCKET_WRITE_SCHEMA`` and belt's
# ``CODE_CHANGE_SCHEMA``). Starts at 1 — this is the first version.
EXTERNAL_ACTION_SCHEMA = 1


def compute_params_hash(action: str, params: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex digest of ``action`` + ``params``.

    Canonical JSON (sorted keys, no whitespace) so the same logical call hashes
    identically regardless of dict ordering. The executor recomputes this off
    the persisted blob and refuses the call if it no longer matches — a human
    approved a SPECIFIC call, and a params edit between propose and approve must
    not silently fire a different one.
    """
    canonical = json.dumps(
        {"action": action, "params": params or {}},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def propose_external_action(
    *,
    workspace_id: str,
    connector_name: str,
    action: str,
    params: dict[str, Any] | None = None,
    requested_by: str,
    scope: str = "workspace",
    pocket_id: str | None = None,
    idempotency_key: str | None = None,
    summary: str | None = None,
    correlation_id: str | None = None,
    assignee: str | None = None,
) -> str:
    """Build + store an Instinct ``Action`` for a gated external-action call.

    Files an Action carrying the ``_external_action`` blob (schema 1) and opens
    the Decision-Graph chain (``agent.proposed``). Returns the proposed Action
    id. NO connector secret is written — the blob carries the connector NAME +
    scope only; the credential is resolved fresh at execution.

    Args:
        workspace_id: the originating tenant. Bound on the router's approve /
            reject paths and re-validated at execution. Required — an external
            action with no tenant to scope it to is unexecutable.
        connector_name: the bound connector to call (e.g. ``"crm"``).
        action: the named connector action to call (e.g. ``"approveApplication"``).
        params: the proposed call params, passed verbatim to the connector
            adapter at execution. Hashed into ``params_hash`` so the executor
            can refuse a post-propose params edit.
        requested_by: the user id that proposed the call (chain actor + audit).
        scope: connector scope — one of ``pocket`` / ``workspace`` / ``user``.
            Defaults to ``workspace``.
        pocket_id: the pocket the connector is bound to, when ``scope="pocket"``.
        idempotency_key: caller-supplied key so the executor never double-fires
            the call. Defaults to a deterministic value derived from the params
            hash when omitted, so an identical re-propose dedupes naturally.
        summary: a human-readable one-liner for the gate UI. A sensible default
            is built from the connector + action when omitted.
        correlation_id: an optional pre-minted chain id. When omitted a fresh one
            is minted here (the common case).
        assignee: the workspace member who should approve. Defaults to
            ``requested_by`` so the proposer's queue carries it.
    """
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_instinct_store

    workspace_id = str(workspace_id or "")
    if not workspace_id:
        raise ValueError("propose_external_action requires a non-empty workspace_id")
    connector_name = str(connector_name or "")
    if not connector_name:
        raise ValueError("propose_external_action requires a non-empty connector_name")
    action = str(action or "")
    if not action:
        raise ValueError("propose_external_action requires a non-empty action")

    call_params = dict(params or {})
    params_hash = compute_params_hash(action, call_params)
    # A deterministic idempotency key when the caller doesn't supply one — keyed
    # on the workspace + connector + params hash so an identical re-propose
    # dedupes at the executor (the executor never double-fires a key it already
    # recorded as executed).
    idem = idempotency_key or f"{workspace_id}:{connector_name}:{action}:{params_hash[:16]}"

    # Mint the chain correlation_id BEFORE building the blob so the stored Action
    # carries it from the first write. The same id threads through approve /
    # reject (router) and execute (executor) so the whole call folds into ONE
    # Decision chain.
    corr = correlation_id or str(uuid4())

    human_summary = summary or (
        f"Call '{action}' on connector '{connector_name}' (scope: {scope})."
    )

    blob: dict[str, Any] = {
        "kind": EXTERNAL_ACTION_KIND,
        "schema": EXTERNAL_ACTION_SCHEMA,
        "workspace_id": workspace_id,
        "connector_name": connector_name,
        "scope": scope,
        "pocket_id": pocket_id,
        "action": action,
        "params": call_params,
        "params_hash": params_hash,
        "idempotency_key": idem,
        "requested_by": requested_by,
        "summary": human_summary,
        # RFC 09 chain-correlation fields (schema 1 carries them from the start).
        "correlation_id": corr,
        "proposed_event_id": None,
    }

    title = f"External action — {connector_action_label(connector_name, action)}"
    recommendation = (
        f"Approve to call '{action}' on connector '{connector_name}' "
        f"(scope: {scope}). {human_summary}"
    )
    trigger = ActionTrigger(
        type="agent",
        source=requested_by or "external_action",
        reason=f"external action '{action}' on connector '{connector_name}' requires approval",
    )

    # ISO: scope the store to the caller's workspace (validated non-empty above)
    # so the proposal lands in the tenant's file — this propose path has no
    # ``current_workspace`` ContextVar set.
    store = get_instinct_store(workspace_id=workspace_id or None)
    action_obj = await store.propose(
        # ``pocket_id`` carries the workspace for external actions — they aren't
        # bound to a pocket the way Mission Control items are (mirrors belt.py).
        # The workspace also rides on the blob (the executor's tenancy gate reads
        # it there); pocket_id mirrors it so the existing per-pocket queries
        # still surface the row.
        pocket_id=workspace_id,
        title=title,
        description=recommendation,
        recommendation=recommendation,
        trigger=trigger,
        category=ActionCategory.EXTERNAL,
        priority=ActionPriority.HIGH,
        parameters={EXTERNAL_ACTION_PARAM_KEY: blob},
        assignee=assignee or requested_by or None,
        workspace_id=workspace_id,
    )

    logger.info(
        "external_action: proposed call '%s' on connector '%s' → Instinct action %s "
        "(workspace=%s, correlation_id=%s)",
        action,
        connector_name,
        action_obj.id,
        workspace_id,
        corr,
    )

    # Open the Decision-Graph chain now that the Action is stored. ``agent.
    # proposed`` is the chain origin; its event id is back-written onto the blob
    # so the router's ``human.corrected`` can cite it as causation. Best-effort:
    # a Decision-Graph wiring failure must NOT fail the propose response.
    proposed_event_id = emit_agent_proposed(
        correlation_id=UUID(corr),
        action_id=action_obj.id,
        kind="external_action",
        intent=f"external action '{action}' on connector '{connector_name}'",
        proposal={"connector": connector_name, "connector_action": action},
        workspace_id=workspace_id,
        user_id=requested_by,
    )
    if proposed_event_id is not None:
        await persist_chain_ids(
            store=store,
            action_id=action_obj.id,
            param_key=EXTERNAL_ACTION_PARAM_KEY,
            correlation_id=corr,
            proposed_event_id=str(proposed_event_id),
        )

    return action_obj.id


def connector_action_label(connector_name: str, action: str) -> str:
    """Build a short, content-free label for the Action title.

    Just ``connector.action`` — no params (a param can carry resolved values an
    operator doesn't need in a title, and a stable label keeps the title the
    same for the same call kind).
    """
    return f"{connector_name}.{action}"


__all__ = [
    "EXTERNAL_ACTION_KIND",
    "EXTERNAL_ACTION_PARAM_KEY",
    "EXTERNAL_ACTION_SCHEMA",
    "compute_params_hash",
    "connector_action_label",
    "propose_external_action",
]
