# ee/cloud/site_plan_requests/propose.py — file an employee's request to put a
# site on a paid plan.
#
# Created: 2026-09-01 (feat/sites-plan-purchase-request).
#
# The propose half of the site-plan request gate. A member publishes and asks for
# a paid tier; ``sites.buy_plan`` (ADMIN) refuses them; instead of ending there,
# the router files THIS — an Instinct Action carrying a ``_site_plan_request``
# blob — and tells them it is waiting on an admin. An admin approves it in The
# Tray and the executor performs the publish that was refused.
#
# This module does NOT charge, publish, or grant anything. It only records what
# was asked for, by whom, at what price.
#
# The blob (schema 1):
#   * ``kind`` / ``schema``    — discriminator + version;
#   * ``workspace_id``         — the tenant. The router's tenancy gate reads it
#                                HERE, and the executor re-validates it;
#   * ``pocket_id``            — the pocket whose site is being published;
#   * ``site_plan_key``        — the CANONICAL tier key being requested. Stored
#                                canonicalized so an approval a week later cannot
#                                resolve a legacy alias to a different rung than
#                                the one the admin read on the card;
#   * ``monthly_price_usd``    — the price AT PROPOSE TIME, recorded for the
#                                approver to read. Never used to charge: the
#                                executor prices from the live catalog, because
#                                the truth about money is the catalog's, not a
#                                week-old snapshot's. It exists so a price that
#                                MOVED between propose and approve is visible
#                                rather than silent (see the executor's drift
#                                check);
#   * ``requested_by``         — the member who asked. Attribution only: this
#                                user is NOT re-checked for purchase rights,
#                                because not having them is the premise;
#   * ``params_hash``          — a stable hash of the request's identity fields,
#                                re-checked at execute time so an approve-with-
#                                edits cannot swap the tier or the pocket out
#                                from under what the admin actually read;
#   * ``idempotency_key``      — so a re-approve never double-publishes;
#   * ``summary``              — one human line for the Tray card;
#   * ``correlation_id`` / ``proposed_event_id`` — the Decision-Graph chain ids.
#
# Security:
#   * Proposing grants NOTHING. The executor checks the APPROVER's current role
#     (see the package docstring for why the approver and not the proposer).
#   * The identity hash is re-checked at execution, so the write that fires is
#     the write that was displayed.
#   * Tenancy is bound on the router's approve / reject paths via
#     ``_assert_site_plan_request_workspace`` and re-validated at execution.

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

# The Instinct Action kind discriminator. The blob also carries this as ``kind``
# for readers that introspect it.
SITE_PLAN_REQUEST_KIND = "site_plan_request"

# The parameters key the blob rides under — peer of ``_admin_action``. The
# router + executor dispatch on this key being present.
SITE_PLAN_REQUEST_PARAM_KEY = "_site_plan_request"

# Schema version. Bump when the blob shape changes so a stale pending request
# approved after a deploy fails loud rather than buying a misread tier.
SITE_PLAN_REQUEST_SCHEMA = 1


def compute_request_hash(workspace_id: str, pocket_id: str, site_plan_key: str) -> str:
    """Return a stable SHA-256 digest of the request's IDENTITY fields.

    Identity, deliberately — not the whole blob. The three fields here are what
    the approver is actually agreeing to: this workspace, this site, this tier.
    The price is excluded on purpose: it is allowed to move between propose and
    approve (the catalog is the source of truth for money), and hashing it would
    turn an ordinary price change into a hard failure instead of the visible
    drift the executor reports.

    Canonical JSON (sorted keys, no whitespace) so ordering cannot change the
    digest.
    """
    canonical = json.dumps(
        {
            "workspace_id": workspace_id,
            "pocket_id": pocket_id,
            "site_plan_key": site_plan_key,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _emit_agent_proposed(
    *,
    correlation_id: UUID,
    action_id: str,
    workspace_id: str,
    pocket_id: str,
    site_plan_key: str,
    user_id: str,
) -> UUID | None:
    """Emit the chain-opening ``agent.proposed`` event for a plan request.

    Mirrors ``admin_proposals.propose._emit_agent_proposed``: the requesting
    member is the actor. Unlike an admin action, this proposal IS bound to a
    pocket, so the chain's ``pocket_id`` carries the real pocket rather than
    standing in with the workspace.

    Returns the emitted event id for the blob's ``proposed_event_id`` (the
    ``human.corrected`` causation handle), or ``None`` when the emit raised —
    best-effort; the reconciler picks up orphans.
    """
    from soul_protocol.spec.journal import Actor

    from pocketpaw_ee.cloud.decisions.journal_writer import record_agent_proposed

    actor = Actor(
        kind="agent",
        id=f"user:{user_id or 'unknown'}",
        scope_context=[f"workspace:{workspace_id}"],
    )
    payload: dict[str, Any] = {
        "intent": f"put this site on the '{site_plan_key}' plan",
        "action": "site_plan_request",
        "pocket_id": pocket_id,
        "inputs": [],
        "proposal_kind": "site_plan_request",
        "proposal": {"site_plan_key": site_plan_key, "pocket_id": pocket_id},
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
            "site_plan_request agent.proposed emit failed for correlation_id=%s "
            "(action_id=%s) — reconciler will catch up",
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
    """Write the chain ids onto the persisted blob after ``agent.proposed`` fired.

    Direct SQL update, the same pattern ``admin_proposals.propose`` uses.
    Best-effort: a failure leaves ``proposed_event_id`` None and the eventual
    ``human.corrected`` emits without a causation_id (the chain still folds).
    """
    import json as _json

    import aiosqlite

    try:
        action = await store.get_action(action_id)
        if action is None:
            return
        params = dict(getattr(action, "parameters", None) or {})
        blob = params.get(SITE_PLAN_REQUEST_PARAM_KEY)
        if not isinstance(blob, dict):
            return
        blob = dict(blob)
        blob["correlation_id"] = correlation_id
        blob["proposed_event_id"] = proposed_event_id
        params[SITE_PLAN_REQUEST_PARAM_KEY] = blob

        async with aiosqlite.connect(store._db_path) as db:
            await db.execute(
                "UPDATE instinct_actions SET parameters = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (_json.dumps(params), action_id),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — write-back is best-effort
        logger.warning(
            "site_plan_request: failed to persist chain ids onto action %s — the "
            "chain's human.corrected will emit without causation_id",
            action_id,
            exc_info=True,
        )


async def propose_site_plan_request(
    *,
    workspace_id: str,
    pocket_id: str,
    site_plan_key: str,
    requested_by: str,
    site_name: str = "",
    idempotency_key: str | None = None,
    correlation_id: str | None = None,
    assignee: str | None = None,
) -> str:
    """File an Instinct Action asking an admin to put this site on a paid plan.

    Returns the proposed Action id. Grants nothing: the executor re-checks the
    APPROVER's role and re-prices from the live catalog before it publishes.

    ``site_plan_key`` is canonicalized here rather than at execute time. A tier
    key can be a legacy alias (``business`` → ``staff``), and the alias table is
    allowed to change; resolving it at propose time means the blob records the
    rung the requester actually saw, and the Tray card, the hash, and the
    eventual purchase all name the same one.

    Raises ``ValueError`` for a missing tenant / pocket / requester, for a tier
    the catalog does not know, and for a tier that is not a per-site rung — an
    org-scoped flat (``studio`` / ``agency``) is not a legal ``site_plan_key`` on
    a publish, so a request for one could never be executed and must not become a
    Tray card that fails on approval.
    """
    from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger
    from pocketpaw.stores import get_instinct_store
    from pocketpaw_ee.cloud.billing import site_plans

    workspace_id = str(workspace_id or "")
    if not workspace_id:
        raise ValueError("propose_site_plan_request requires a non-empty workspace_id")
    pocket_id = str(pocket_id or "")
    if not pocket_id:
        raise ValueError("propose_site_plan_request requires a non-empty pocket_id")
    requested_by = str(requested_by or "")
    if not requested_by:
        raise ValueError("propose_site_plan_request requires a non-empty requested_by")

    # ``site_scoped_tier`` rather than ``get_site_plan``: it resolves a legacy
    # alias AND returns None for an ORG-scoped flat (studio / agency), which is
    # exactly the refusal wanted here. An org key is not a legal ``site_plan_key``
    # on a publish, so a request for one could only ever become a Tray card that
    # fails on approval — refuse it at the door, where the requester is present to
    # be told why. Using the same helper the entitlement seams use also means this
    # cannot drift from their idea of what a per-site rung is.
    canonical_key = site_plans.canonical_site_tier_key(str(site_plan_key or ""))
    tier = site_plans.site_scoped_tier(canonical_key)
    if tier is None:
        raise ValueError(f"'{site_plan_key}' is not a plan a single site can be put on")
    monthly_price_usd = int(tier.monthly_price_usd or 0)

    request_hash = compute_request_hash(workspace_id, pocket_id, canonical_key)
    idem = idempotency_key or f"{workspace_id}:{pocket_id}:{request_hash[:16]}"
    corr = correlation_id or str(uuid4())

    tier_label = getattr(tier, "display_name", "") or canonical_key
    subject = site_name or "this site"
    human_summary = (
        f"Put {subject} on the {tier_label} plan "
        f"(${monthly_price_usd}/month, added to this workspace's subscription)."
    )

    blob: dict[str, Any] = {
        "kind": SITE_PLAN_REQUEST_KIND,
        "schema": SITE_PLAN_REQUEST_SCHEMA,
        "workspace_id": workspace_id,
        "pocket_id": pocket_id,
        "site_plan_key": canonical_key,
        "monthly_price_usd": monthly_price_usd,
        "requested_by": requested_by,
        "params_hash": request_hash,
        "idempotency_key": idem,
        "summary": human_summary,
        "correlation_id": corr,
        "proposed_event_id": None,
    }

    recommendation = (
        f"Approving adds ${monthly_price_usd}/month to this workspace's "
        f"subscription and publishes {subject} on the {tier_label} plan. "
        "Rejecting leaves the site on its current plan."
    )
    trigger = ActionTrigger(
        type="user",
        source=requested_by,
        reason="a workspace member asked to put a site on a paid plan",
    )

    # Scope the store to the tenant — this path has no ``current_workspace``
    # ContextVar set.
    store = get_instinct_store(workspace_id=workspace_id or None)
    action_obj = await store.propose(
        pocket_id=pocket_id,
        title=f"Site plan request — {tier_label}",
        description=human_summary,
        recommendation=recommendation,
        trigger=trigger,
        category=ActionCategory.WORKFLOW,
        # HIGH, like an admin action: someone is blocked on the answer. A request
        # that sits in the Tray unnoticed is the 403 again, only slower.
        priority=ActionPriority.HIGH,
        parameters={SITE_PLAN_REQUEST_PARAM_KEY: blob},
        # NOT the requester: they cannot approve this (that is the premise). Left
        # unassigned so it reaches the workspace's admins rather than sitting in
        # the inbox of the one person who is known to be unable to act on it.
        assignee=assignee,
        workspace_id=workspace_id,
    )

    logger.info(
        "site_plan_request: proposed tier '%s' for pocket %s → Instinct action %s "
        "(workspace=%s, requested_by=%s, correlation_id=%s)",
        canonical_key,
        pocket_id,
        action_obj.id,
        workspace_id,
        requested_by,
        corr,
    )

    proposed_event_id = _emit_agent_proposed(
        correlation_id=UUID(corr),
        action_id=action_obj.id,
        workspace_id=workspace_id,
        pocket_id=pocket_id,
        site_plan_key=canonical_key,
        user_id=requested_by,
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
    "SITE_PLAN_REQUEST_KIND",
    "SITE_PLAN_REQUEST_PARAM_KEY",
    "SITE_PLAN_REQUEST_SCHEMA",
    "compute_request_hash",
    "propose_site_plan_request",
]
