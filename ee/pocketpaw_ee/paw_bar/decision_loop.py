# ee/paw_bar/decision_loop.py — Close the customer decision loop via Instinct.
# Updated: 2026-07-15 (B0 H1 — store-split tenancy fix) — propose_customer_decision
#   now routes the write through the PER-WORKSPACE store the cloud dashboard reads
#   from — get_instinct_store(workspace_id=widget.workspace_id) — instead of the
#   bare get_instinct_store(). Root cause: the widget carries a real, server-stamped
#   workspace_id (the header's old "no workspace_id column" claim was STALE), but
#   resolve_workspace_id returned the widget OWNER and the write used the bare
#   factory. In cloud/flag mode the workspace-less public ingest path made that bare
#   factory raise WorkspaceScopeRequired → swallowed → the proposal was DROPPED
#   (invisible to the owner's Tray); in shared mode it was stamped in-row with the
#   owner label the dashboard never filters by. Now: real workspace_id = store route
#   + in-row scope; owner = assignee only. See resolve_workspace_id + the store block.
# Updated: 2026-07-08 — Renamed widget "Paw Print" → "Paw Bar" (module paw_print→paw_bar,
#   source/requested_by labels paw_print→paw_bar). The separate one-word audit feed
#   (past-tense record) is a DIFFERENT feature, unaffected.
# Created: 2026-06-11 (gap2) — The missing back-half of the Paw Bar loop.
#   Before this module, an inbound widget/site event became a Fabric object and
#   STOPPED: no proposal, no human approval, no decision delivered back to the
#   customer. This module wires the closed loop:
#
#     customer event → propose_customer_decision()  (raise an Instinct proposal
#       carrying the event context + a default reply + a parked PENDING
#       DecisionStatus row)
#         → the proposal lands in the workspace-scoped pending list / The Tray
#           (existing Instinct surface — no new UI needed)
#         → a human APPROVES (or rejects) via the existing instinct router
#           → deliver_customer_decision()  (flip the parked DecisionStatus to
#             delivered/declined with the human's reply)
#         → the customer surface polls get_latest_decision and reads the answer.
#
# Why a separate module (mirrors cloud/pockets/instinct_bridge.py): the
#   proposal/deliver functions sit between the OSS PawBarStore and the OSS
#   InstinctStore. They are allowed to be impure (they call both stores), and
#   keeping them out of the HTTP router lets the instinct router import the
#   deliver hook directly without dragging in FastAPI.
#
# Tenancy: the proposal's ``_customer_reply`` blob carries ``workspace_id`` so
#   the Instinct Action is workspace-scoped exactly like a parked pocket write.
#   The tenant boundary is the widget's real ``workspace_id`` (stamped server-side
#   at create from the authenticated session — the SAME token the dashboard reads
#   its pending feed by), which ALSO routes the physical instinct.db. The widget
#   OWNER is a within-tenant human label used as the assignee (and as a back-compat
#   in-row scope for a legacy widget whose ``workspace_id`` is still empty). The
#   ``_customer_reply`` approve/reject paths carry a workspace assertion
#   (``_assert_customer_reply_workspace``) comparing the blob's ``workspace_id`` to
#   the caller's active workspace, like every other gated blob kind.
#
# Security: the blob carries NO secret — only the widget id, customer_ref,
#   event type/payload-summary, workspace, and the default reply text. The
#   delivery hook re-resolves the parked row by Instinct action id (the stable
#   join key) so a tampered blob cannot redirect a decision to another
#   customer's row.
#
# Updated: 2026-06-11 (gap-housekeeping) — propose_customer_decision now SKIPS
#   the loop when the widget resolves to an EMPTY workspace (owner unset). A
#   NULL-scoped Instinct proposal would surface in every tenant's pending list,
#   so an owner-less widget logs a warning and raises NO proposal rather than an
#   all-tenant-visible one. The event + Fabric object still persist; the loop
#   simply doesn't open until the owner is set. Chose the guard over a hard
#   PawBarWidget.owner field-validator because the owner may legitimately be
#   assigned after the widget is created.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Schema version stamped onto the ``_customer_reply`` blob. Bump when the blob
# shape changes so a stale pending Action approved after a deploy is handled
# loudly rather than misinterpreted. (Mirrors instinct_bridge._POCKET_WRITE_SCHEMA.)
_CUSTOMER_REPLY_SCHEMA = 1

# The blob key the Instinct Action carries under ``parameters``. The instinct
# router dispatches the delivery hook on this key's presence, exactly as it
# dispatches the pocket-write bridge on ``_pocket_write``.
CUSTOMER_REPLY_KEY = "_customer_reply"

_MAX_SUMMARY_CHARS = 280


def resolve_workspace_id(widget: Any) -> str:
    """Resolve the owning workspace (tenancy scope) for a Paw Bar widget.

    The widget carries a real ``workspace_id`` — stamped server-side at create
    time from the authenticated session (``create_widget`` /
    ``current_workspace_id``), the SAME token the cloud dashboard reads its
    pending feed by. That is the tenant boundary, so prefer it. Fall back to the
    OWNER only for a legacy / single-tenant widget whose ``workspace_id`` is
    still empty (pre-tenancy rows); the owner is a within-tenant human label
    (possibly colon-qualified, ``user:maya``) and is used as the in-row scope in
    that back-compat case exactly as before.

    This is the in-row / blob tenancy scope. Store ROUTING (which physical
    instinct.db) is done separately in ``propose_customer_decision`` from the
    real ``workspace_id`` only — an owner label is never a store-path token.
    """
    return str(getattr(widget, "workspace_id", "") or "") or str(
        getattr(widget, "owner", "") or ""
    )


def _summarize_payload(payload: dict[str, Any]) -> str:
    """One-line, length-capped human summary of an event payload for the proposal.

    The proposal title/recommendation shows the operator WHAT the customer asked
    without dumping an arbitrarily large payload into the Action. Truncated hard
    at ``_MAX_SUMMARY_CHARS`` so a large (already size-capped at ingest) payload
    can't bloat the Action row.
    """
    import json as _json

    try:
        text = _json.dumps(payload, default=str, sort_keys=True)
    except Exception:  # noqa: BLE001 — summary must never raise into ingest
        text = str(payload)
    if len(text) > _MAX_SUMMARY_CHARS:
        return text[: _MAX_SUMMARY_CHARS - 1] + "…"
    return text


def _default_reply(event_type: str) -> str:
    """The pre-filled reply the operator can accept as-is or edit on approval.

    Kept deliberately generic — the operator owns the final wording. Editing it
    flows through the existing Instinct correction path (the edit is captured as
    a Correction and the edited recommendation is what gets delivered).
    """
    return f"Thanks for your '{event_type}' request — we've received it and will follow up shortly."


async def propose_customer_decision(
    *,
    widget: Any,
    event: Any,
    paw_bar_store: Any,
    requested_by: str = "paw_bar",
) -> str | None:
    """Raise an Instinct proposal for one inbound customer event + park a row.

    ``widget`` is the :class:`PawBarWidget`; ``event`` is the accepted
    :class:`PawBarEvent`. Creates:

      1. An Instinct ``Action`` (category EXTERNAL) carrying a ``_customer_reply``
         blob with the event context + a default reply + the workspace scope. The
         Action lands in the owner's workspace-scoped pending list / The Tray.
      2. A PENDING :class:`DecisionStatus` row keyed to that Action id, so the
         customer surface immediately polls "we're looking into it" and the
         approve hook has a stable row to flip.

    Returns the proposed Action id, or ``None`` if the proposal could not be
    raised (best-effort — a decision-loop failure must NEVER fail the ingest
    response; the event + Fabric object already persisted).
    """
    try:
        from pocketpaw.instinct.models import ActionCategory, ActionTrigger
        from pocketpaw.paw_bar.models import DecisionState, DecisionStatus
        from pocketpaw.stores import get_instinct_store

        widget_id = str(getattr(widget, "id", "") or "")
        pocket_id = str(getattr(widget, "pocket_id", "") or "")
        # Two DISTINCT identities on the widget (the H1 bug conflated them):
        #   * ``store_ws`` — the real, server-stamped ``workspace_id`` (a
        #     store-path-safe token). It routes the physical instinct.db AND is
        #     the in-row tenancy scope, so the proposal lands in the SAME
        #     per-workspace store the cloud dashboard reads its pending feed from.
        #   * ``owner`` — the within-tenant human label (possibly colon-qualified,
        #     ``user:maya``). It is the ASSIGNEE (routes The Tray to that human),
        #     never a store-path token.
        store_ws = str(getattr(widget, "workspace_id", "") or "")
        owner = str(getattr(widget, "owner", "") or "")
        # In-row / blob tenancy scope: the real workspace, or the owner for a
        # legacy widget whose workspace_id is still empty (back-compat).
        workspace_id = resolve_workspace_id(widget)
        # Tenancy guard: an owner-less widget resolves to an EMPTY workspace. A
        # proposal raised with no workspace scope is NULL-scoped in Instinct, so
        # it would appear in EVERY tenant's pending list / The Tray — a
        # cross-tenant leak that also lets any operator approve another's
        # customer request. Skip the loop entirely rather than open it
        # mis-scoped: log a warning and leave the event + Fabric object (already
        # persisted) as the only record. The owner can be set later and a re-sent
        # event will then open the loop correctly. This is deliberately the
        # less-invasive of the two options in the review note — a hard
        # field-validator on PawBarWidget.owner would reject a widget whose
        # owner is legitimately assigned after creation.
        if not workspace_id.strip():
            logger.warning(
                "paw-bar widget %s has no owner/workspace — SKIPPING the "
                "decision proposal so it is not raised NULL-scoped and visible "
                "to every tenant. Set the widget owner to open the loop.",
                widget_id or "<unknown>",
            )
            return None
        customer_ref = str(getattr(event, "customer_ref", "") or "")
        event_type = str(getattr(event, "type", "") or "")
        payload = getattr(event, "payload", None) or {}

        widget_name = str(getattr(widget, "name", "") or "") or widget_id
        summary = _summarize_payload(payload)
        default_reply = _default_reply(event_type)

        title = f"Customer request on {widget_name}: {event_type}".strip()
        recommendation = (
            f"A customer ({customer_ref}) sent a '{event_type}' request via the "
            f"'{widget_name}' widget. Suggested reply: {default_reply}"
        )
        description = f"Request payload: {summary}"

        trigger = ActionTrigger(
            type="connector",
            source=f"paw_bar:{widget_id}",
            reason=f"customer '{event_type}' event awaiting a human decision",
        )

        # The blob carries NO secret — only routing + context + the editable
        # reply. ``reply`` is the default; if the operator edits the Action's
        # recommendation on approval, the delivery hook reads the (edited)
        # recommendation off the approved Action, so the customer gets the
        # operator's final wording.
        blob = {
            "schema": _CUSTOMER_REPLY_SCHEMA,
            "widget_id": widget_id,
            "pocket_id": pocket_id,
            "customer_ref": customer_ref,
            "event_type": event_type,
            "workspace_id": workspace_id,
            "default_reply": default_reply,
            "payload_summary": summary,
        }

        # ISO-2 store routing (the H1 fix): route the write through the SAME
        # per-workspace factory the cloud dashboard reads from —
        # ``get_instinct_store(workspace_id=<real workspace>)`` lands the Action
        # in ``~/.pocketpaw/workspaces/<store_ws>/instinct.db``, the exact file
        # ``GET /instinct/actions/pending`` resolves for that tenant. The real
        # ``workspace_id`` is a store-path-safe token, so no allowlist ValueError.
        # A legacy widget with an empty ``store_ws`` keeps the BARE store +
        # in-row scoping (owner as the scope) exactly as before — the owner label
        # is never threaded into the store factory (it would ValueError on ``:``).
        # Previously this called the bare ``get_instinct_store()`` unconditionally,
        # which in cloud/flag mode raised ``WorkspaceScopeRequired`` on the
        # workspace-less public ingest path → the proposal was silently dropped.
        store = get_instinct_store(workspace_id=store_ws or None)
        action = await store.propose(
            pocket_id=pocket_id or widget_id,
            title=title,
            description=description,
            recommendation=recommendation,
            trigger=trigger,
            category=ActionCategory.EXTERNAL,
            parameters={CUSTOMER_REPLY_KEY: blob},
            # Workspace-scope the Action so it appears only in the owning
            # tenant's pending list (deny-by-default Instinct tenancy — W4a).
            workspace_id=workspace_id or None,
            # Route the approval to the widget OWNER (the human who owns the
            # customer relationship), NOT the workspace — The Tray filters by
            # assignee to show an operator only the items they own.
            assignee=owner or None,
        )

        # Park the PENDING decision row so the customer surface has something to
        # poll immediately and the approve hook has a row to flip by action id.
        decision = DecisionStatus(
            widget_id=widget_id,
            customer_ref=customer_ref,
            event_type=event_type,
            instinct_action_id=str(action.id),
            workspace_id=workspace_id,
            state=DecisionState.PENDING,
            reply="",
        )
        await paw_bar_store.create_decision(decision)

        logger.info(
            "paw-bar event on widget %s (customer %s) → Instinct proposal %s "
            "(workspace=%s) + parked PENDING decision",
            widget_id,
            customer_ref,
            action.id,
            workspace_id or "<owner-unset>",
        )
        return str(action.id)
    except Exception:  # noqa: BLE001 — the loop is best-effort over ingest
        logger.warning(
            "failed to raise customer-decision proposal for widget %s — the "
            "event + Fabric object already persisted; the loop just didn't open",
            getattr(widget, "id", "<unknown>"),
            exc_info=True,
        )
        return None


def customer_reply_blob(action: Any) -> dict[str, Any] | None:
    """Return the ``_customer_reply`` blob on an Action, or ``None``.

    Mirror of ``instinct_bridge`` / ``_code_change_blob`` — the instinct router
    dispatches the delivery hook on this blob's presence. Anything that is not a
    dict is treated as "no customer reply".
    """
    params = getattr(action, "parameters", None)
    if not isinstance(params, dict):
        return None
    blob = params.get(CUSTOMER_REPLY_KEY)
    return blob if isinstance(blob, dict) else None


async def deliver_customer_decision(action: Any, *, declined: bool = False) -> None:
    """Deliver the human's decision back to the customer surface.

    Called best-effort from the instinct router's approve / reject handlers
    after the Action status flips. Flips the parked :class:`DecisionStatus` row
    (resolved by Instinct action id — the stable join key) to:

      * DELIVERED with the operator's reply, on approve. The reply is taken from
        the approved Action's ``recommendation`` if the operator edited it (the
        edited wording is what the customer should read), else the blob's
        ``default_reply``.
      * DECLINED with the rejection reason, on reject.

    Never raises — a delivery failure must not break the approve/reject response.
    The row simply stays PENDING and a retry / sweep can re-resolve it later.
    """
    try:
        from pocketpaw.paw_bar.models import DecisionState
        from pocketpaw.stores import get_paw_bar_store

        blob = customer_reply_blob(action)
        if blob is None:
            return
        if blob.get("schema") != _CUSTOMER_REPLY_SCHEMA:
            logger.warning(
                "customer-reply blob on action %s is from an incompatible build "
                "(schema=%s) — not delivering",
                getattr(action, "id", "<unknown>"),
                blob.get("schema"),
            )
            return

        action_id = str(getattr(action, "id", "") or "")
        decided_by = str(getattr(action, "approved_by", "") or "") or "system"

        if declined:
            state = DecisionState.DECLINED
            reply = str(getattr(action, "rejected_reason", "") or "") or (
                "We're unable to action this request right now."
            )
        else:
            state = DecisionState.DELIVERED
            # Prefer the operator's (possibly edited) recommendation — that is
            # the human's final wording. Fall back to the default reply.
            reply = str(getattr(action, "recommendation", "") or "") or str(
                blob.get("default_reply") or ""
            )

        # The blob carries the owning workspace; thread it so set_decision flips
        # only a row inside that tenant. An empty blob workspace passes None
        # (unscoped) — matching how the proposal would have been raised.
        blob_workspace = str(blob.get("workspace_id") or "") or None

        store = get_paw_bar_store()
        updated = await store.set_decision(
            action_id,
            state=state,
            reply=reply,
            decided_by=decided_by,
            workspace_id=blob_workspace,
        )
        if updated is None:
            logger.info(
                "no parked decision row for action %s — nothing to deliver "
                "(proposal may predate the decision-loop slice)",
                action_id,
            )
            return
        logger.info(
            "delivered customer decision for action %s → %s (widget %s, customer %s)",
            action_id,
            state.value,
            updated.widget_id,
            updated.customer_ref,
        )
    except Exception:  # noqa: BLE001 — delivery is best-effort over approve/reject
        logger.warning(
            "failed to deliver customer decision for action %s — the parked row "
            "stays pending; a later approve replay or sweep re-resolves it",
            getattr(action, "id", "<unknown>"),
            exc_info=True,
        )


__all__ = [
    "CUSTOMER_REPLY_KEY",
    "customer_reply_blob",
    "deliver_customer_decision",
    "propose_customer_decision",
    "resolve_workspace_id",
]
