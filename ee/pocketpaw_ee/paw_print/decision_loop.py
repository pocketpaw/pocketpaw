# ee/paw_print/decision_loop.py — Close the customer decision loop via Instinct.
# Created: 2026-06-11 (gap2) — The missing back-half of the Paw Print loop.
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
#   proposal/deliver functions sit between the OSS PawPrintStore and the OSS
#   InstinctStore. They are allowed to be impure (they call both stores), and
#   keeping them out of the HTTP router lets the instinct router import the
#   deliver hook directly without dragging in FastAPI.
#
# Tenancy: the proposal's ``_customer_reply`` blob carries ``workspace_id`` so
#   the Instinct Action is workspace-scoped exactly like a parked pocket write.
#   paw_print's widget model predates per-row tenancy and has no workspace_id
#   column, so the workspace is resolved from the widget OWNER (the freelancer /
#   operator who owns the widget IS the tenant boundary here). This is the same
#   scoping key the paw_print store already indexes on. When the widget model
#   grows a real workspace_id this resolver is the one line to change.
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
#   PawPrintWidget.owner field-validator because the owner may legitimately be
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
    """Resolve the owning workspace for a Paw Print widget.

    The widget model predates per-row tenancy and has no ``workspace_id``; the
    OWNER (the operator who created the widget) is the tenant boundary, and it is
    the column the paw_print store already scopes on. A widget with a
    colon-qualified owner (``user:maya``) keeps the full string — the instinct
    workspace assertion compares the blob's ``workspace_id`` to the caller's
    active workspace as an opaque string, so consistency, not format, is what
    matters here.
    """
    return str(getattr(widget, "owner", "") or "")


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
    paw_print_store: Any,
    requested_by: str = "paw_print",
) -> str | None:
    """Raise an Instinct proposal for one inbound customer event + park a row.

    ``widget`` is the :class:`PawPrintWidget`; ``event`` is the accepted
    :class:`PawPrintEvent`. Creates:

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
        from pocketpaw.paw_print.models import DecisionState, DecisionStatus
        from pocketpaw.stores import get_instinct_store

        widget_id = str(getattr(widget, "id", "") or "")
        pocket_id = str(getattr(widget, "pocket_id", "") or "")
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
        # field-validator on PawPrintWidget.owner would reject a widget whose
        # owner is legitimately assigned after creation.
        if not workspace_id.strip():
            logger.warning(
                "paw-print widget %s has no owner/workspace — SKIPPING the "
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
            source=f"paw_print:{widget_id}",
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

        store = get_instinct_store()
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
            # Route the approval to the widget owner — the operator who owns the
            # customer relationship is the human in the loop.
            assignee=workspace_id or None,
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
        await paw_print_store.create_decision(decision)

        logger.info(
            "paw-print event on widget %s (customer %s) → Instinct proposal %s "
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
        from pocketpaw.paw_print.models import DecisionState
        from pocketpaw.stores import get_paw_print_store

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

        store = get_paw_print_store()
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
