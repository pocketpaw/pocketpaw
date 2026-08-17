# ee/paw_bar/decision_loop.py — Close the customer decision loop via Instinct.
# Updated: 2026-08-01 (AL-2, paw-bar emitters) — ``deliver_customer_decision``
#   now records the DELIVERED beat (``paw.action.delivered``) through
#   ``paw_bar/ledger.py``. AL-1 gave the funnel its proposal and its approval;
#   without this the board could show that a human said yes but never that the
#   visitor got the answer — which is the only step the customer experiences.
#   Three deliberate choices, each of which was a way to get this wrong:
#     * ONLY on the DELIVERED path. A decline already lands AL-1's
#       ``paw.action.rejected`` row, so emitting on both would count one refusal
#       twice and quietly inflate the funnel's last stage.
#     * The FILE is routed by the widget's real ``workspace_id`` — the same token
#       ``propose_customer_decision`` routes the instinct.db by — while the row's
#       in-row scope is the blob's workspace (the Action's own scope). This file
#       has kept those two values apart since the H1 fix for exactly this reason:
#       the blob's value falls back to the colon-qualified OWNER label, which the
#       store factory rejects, and a rejected route inside a fail-soft emitter is
#       a row that vanishes with nobody told. Approved row and delivered row now
#       land in the same file AND the same bucket.
#     * The widget lookup moved OUT of the email block so both consumers share
#       one read; the email path is otherwise untouched.
# Updated: 2026-07-31 (AL-1, agent ledger spine) — both propose paths now stamp
#   ``actor_agent_id`` on the Action from the widget's bound agent, via the new
#   ``resolve_widget_agent`` helper. Until now the only trace of WHICH agent
#   raised a paw-bar proposal was the ``paw_bar:<widget_id>`` trigger string:
#   readable by a person, not joinable by a query. The ledger emitter keys every
#   approval on the Action's ``actor_agent_id``, so an unstamped proposal would
#   land in the unattributed bucket and the concierge's own value board would
#   show nothing. The binding already existed on the widget (``widget.agent_id``,
#   set by agent_provisioning) — all that was missing was carrying it across.
#   The helper is fail-soft in the ``notify.py`` shape: a widget with no agent,
#   or a duck-typed object without the attribute, yields "" and the proposal is
#   raised exactly as before.
# Updated: 2026-07-30 (async decision delivery) — deliver_customer_decision now
#   closes the loop for a visitor who LEFT the page: if the flipped row carries
#   a ``contact_email`` (attached via POST /paw-bar/decision-contact while the
#   row was pending), it sends ONE email with the SAME customer-facing reply
#   the poll returns. Sent only on the PENDING → decided transition (the prior
#   row state is read before the flip), so an approve replay / re-delivery
#   never re-sends. Fail-soft via paw_bar.mailer — a mail failure (or no SMTP
#   transport at all) logs and moves on; the poll delivery is untouched. The
#   email address never leaves the DecisionStatus row (PII invariant — see
#   models.DecisionStatus.contact_email).
# Updated: 2026-07-30 (visitor-reply leak) — both proposal paths pre-filled
#   ``recommendation`` with the OWNER-facing framing ("A visitor (ref …) asked …
#   untrusted input … Suggested reply: …"), and deliver_customer_decision sends
#   ``recommendation`` to the visitor VERBATIM on approve — so an unedited
#   approval leaked the internal analysis text to the customer surface. Now
#   ``recommendation`` carries ONLY the editable customer-facing default reply,
#   and the framing/context moved into ``description`` (Tray-only). Found live
#   in the 2026-07-30 demo smoke.
# Updated: 2026-07-16 (C1 hardening) — propose_customer_action now sanitizes the
#   visitor-controlled portions of the owner-facing proposal: customer_ref + the
#   args summary are run through _sanitize_for_human (strip control chars, collapse
#   whitespace, length-cap — the summary at the same 280 chars the event path uses,
#   which this path previously skipped) and DEMARCATED from our framing as
#   "untrusted input" so a human approver can't be misled by injected text.
#   Backend defense-in-depth; the Tray frontend should also escape on render.
# Updated: 2026-07-16 (Paw Bar action registry, C1) — added
#   ``propose_customer_action``: the gated-verb path. When a concierge action's
#   policy is "gated", the shared executor NEVER runs the verb — it raises an
#   Instinct proposal (kind "paw_bar_action") carrying the widget/customer/verb/
#   args + a human-readable summary, and parks a PENDING DecisionStatus row keyed
#   to the action id. It reuses the SAME ``_customer_reply`` blob schema + parked
#   row as ``propose_customer_decision``, so the existing ``deliver_customer_decision``
#   approve/reject hook (instinct router) flips the row unchanged and the visitor
#   reads the outcome on the SAME decision poll. Owner-less widget → no proposal
#   (same NULL-scope guard). The proposal is the only effect (SS-2).
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
import re
from typing import Any

logger = logging.getLogger(__name__)


def _sanitize_for_human(text: Any, *, cap: int) -> str:
    """Neutralize visitor-controlled text before it lands in an owner-facing
    Instinct proposal (C1 hardening).

    Strips control characters (so an embedded newline can't inject fake framing
    into the human approver's Tray view), collapses runs of whitespace, and caps
    the length. This is BACKEND defense-in-depth; the Tray frontend should ALSO
    escape on render (a separate paw-enterprise follow-up)."""
    s = "".join(ch for ch in str(text) if ch.isprintable())
    s = re.sub(r"\s+", " ", s).strip()
    return s[:cap]


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
    return str(getattr(widget, "workspace_id", "") or "") or str(getattr(widget, "owner", "") or "")


def resolve_widget_agent(widget: Any) -> str:
    """The agent id bound to a Paw Bar widget, or "" when there isn't one (AL-1).

    A site concierge IS a normal agent: ``agent_provisioning`` binds one and
    stamps ``widget.agent_id``, which is the same key the agent-scoped inbox and
    the notification deep-link already resolve by. This reads that binding so a
    proposal can carry its proposer into the ledger.

    Fail-soft by construction — ``getattr`` with a default, coerced through
    ``str``. An unbound widget, a legacy widget written before the column, or a
    duck-typed stand-in in a test all return "", and "" is a legal
    ``actor_agent_id``: the proposal still gets raised, the approval still gets
    recorded, and the row simply counts as unattributed. Attribution is worth
    having, never worth failing a customer's request over.
    """
    return str(getattr(widget, "agent_id", "") or "")


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
        # ``recommendation`` is the editable, CUSTOMER-FACING reply — the approve
        # hook (deliver_customer_decision) sends it to the visitor VERBATIM, so
        # it must never carry the owner-facing framing. That context lives in
        # ``description``, which stays inside the Tray.
        recommendation = default_reply
        description = (
            f"A customer ({customer_ref}) sent a '{event_type}' request via the "
            f"'{widget_name}' widget. Request payload: {summary}"
        )

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
            # AL-1 — the AGENT that raised this, so the approval lands on that
            # concierge's ledger. Distinct from ``assignee`` (the human who
            # decides) and from ``workspace_id`` (the tenant): three identities,
            # three jobs, and conflating any two of them is what made the
            # concierge funnel un-queryable in the first place.
            actor_agent_id=resolve_widget_agent(widget),
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


async def propose_customer_action(
    *,
    widget: Any,
    workspace_id: str,
    customer_ref: str,
    verb: str,
    args: dict[str, Any],
    summary: str,
    paw_bar_store: Any,
) -> str | None:
    """Raise an Instinct proposal for a GATED concierge action + park a row.

    The gated-verb half of the action registry (C1). The shared executor calls
    this INSTEAD of running the verb: a gated action never executes an effect —
    it hands a human the decision. Creates the SAME two artifacts as
    ``propose_customer_decision`` so the existing approve/reject delivery hook
    works unchanged:

      1. An Instinct ``Action`` (category EXTERNAL) carrying a ``_customer_reply``
         blob. The blob adds ``kind="paw_bar_action"`` + ``verb`` + ``args`` for
         auditability, but keeps the schema + routing fields
         (``deliver_customer_decision`` reads only those), so approval flips the
         parked row exactly like an ingest decision.
      2. A PENDING :class:`DecisionStatus` row keyed to the action id, event_type
         ``paw_bar_action:<verb>``, so the visitor polls the SAME decision endpoint
         and reads "we're looking into it" until a human decides.

    ``workspace_id`` is the widget's resolved workspace (passed by the executor,
    which resolved it from the run). Fails CLOSED on a blank workspace (no
    NULL-scoped proposal). Returns the Action id, or ``None`` when no proposal
    could be raised (best-effort — never raises into the action response).
    """
    try:
        from pocketpaw.instinct.models import ActionCategory, ActionTrigger
        from pocketpaw.paw_bar.models import DecisionState, DecisionStatus
        from pocketpaw.stores import get_instinct_store

        widget_id = str(getattr(widget, "id", "") or "")
        pocket_id = str(getattr(widget, "pocket_id", "") or "")
        ws = str(workspace_id or "").strip()
        # Same NULL-scope guard as propose_customer_decision: an owner/workspace-
        # less widget would raise an all-tenant-visible proposal, so skip it.
        if not ws:
            logger.warning(
                "paw-bar action %s on widget %s has no workspace — SKIPPING the "
                "gated proposal so it is not raised NULL-scoped.",
                verb,
                widget_id or "<unknown>",
            )
            return None

        widget_name = str(getattr(widget, "name", "") or "") or widget_id
        event_type = f"paw_bar_action:{verb}"
        # Neutralize + cap the visitor-controlled portions before they land in the
        # owner-facing proposal. ``verb`` is snake_case-validated at spec time, but
        # ``customer_ref`` and the args ``summary`` are visitor input — sanitize
        # them and DEMARCATE them from our framing so a human approver can never
        # be misled by injected text. The composed summary is capped at the same
        # 280 chars the event path uses (which this path previously skipped).
        safe_customer = _sanitize_for_human(customer_ref, cap=128)
        safe_summary = _sanitize_for_human(summary, cap=_MAX_SUMMARY_CHARS)
        safe_widget_name = _sanitize_for_human(widget_name, cap=80) or widget_id
        default_reply = (
            f"Thanks, we've passed your '{verb}' request to the team and will follow up shortly."
        )
        title = f"Visitor action on {safe_widget_name}: {verb}".strip()
        # Same contract as the event path above: ``recommendation`` is delivered
        # to the visitor VERBATIM on approve, so it carries ONLY the editable
        # customer-facing reply; the owner-facing analysis stays in
        # ``description`` (Tray-only).
        recommendation = default_reply
        description = (
            f"A visitor (ref {safe_customer}) asked to run the '{verb}' action via "
            f"the '{safe_widget_name}' concierge. Visitor-provided details "
            f"(untrusted input): [{safe_summary}]"
        )

        trigger = ActionTrigger(
            type="connector",
            source=f"paw_bar:{widget_id}",
            reason=f"visitor '{verb}' action awaiting a human decision",
        )
        blob = {
            "schema": _CUSTOMER_REPLY_SCHEMA,
            "kind": "paw_bar_action",
            "widget_id": widget_id,
            "pocket_id": pocket_id,
            "customer_ref": customer_ref,
            "event_type": event_type,
            "verb": verb,
            "args": args,
            "workspace_id": ws,
            "default_reply": default_reply,
            "payload_summary": summary,
        }
        # ISO-2 store routing — the SAME per-workspace factory the event path
        # uses (H1). The bare factory only resolved the right store when an
        # AGENT run's ContextVars carried the workspace; the PUBLIC
        # POST /paw-bar/action endpoint has no run context, so a form-card
        # submit landed its proposal in the BARE instinct.db — invisible to the
        # owner's workspace-scoped Tray/dashboard (found live 2026-07-30, first
        # exercise of the endpoint path off an agent run).
        store = get_instinct_store(workspace_id=ws or None)
        action = await store.propose(
            pocket_id=pocket_id or widget_id,
            title=title,
            description=description,
            recommendation=recommendation,
            trigger=trigger,
            category=ActionCategory.EXTERNAL,
            parameters={CUSTOMER_REPLY_KEY: blob},
            workspace_id=ws or None,
            # Same split as the event path above: the WORKSPACE scopes the row,
            # the OWNER is the assignee. Passing the workspace id as the assignee
            # (as this path did) hides a form-card submit from the operator's
            # "assigned to me" Tray filter — a workspace id is not a user identity,
            # so it matches no operator. The event path has always routed to the
            # owner; the two must agree, or the same visitor raises two
            # differently-routed proposals depending on which path caught them.
            assignee=str(getattr(widget, "owner", "") or "") or None,
            # AL-1 — same attribution as the event path above. The two paths must
            # agree here for the same reason they must agree on the assignee: one
            # visitor should not produce two differently-attributed proposals
            # depending on which entry point caught them.
            actor_agent_id=resolve_widget_agent(widget),
        )
        decision = DecisionStatus(
            widget_id=widget_id,
            customer_ref=customer_ref,
            event_type=event_type,
            instinct_action_id=str(action.id),
            workspace_id=ws,
            state=DecisionState.PENDING,
            reply="",
        )
        await paw_bar_store.create_decision(decision)
        logger.info(
            "paw-bar gated action '%s' on widget %s (visitor %s) → Instinct "
            "proposal %s (workspace=%s) + parked PENDING decision",
            verb,
            widget_id,
            customer_ref,
            action.id,
            ws,
        )
        return str(action.id)
    except Exception:  # noqa: BLE001 — the proposal is best-effort over the action
        logger.warning(
            "failed to raise gated-action proposal '%s' for widget %s",
            verb,
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
        # Read the PRIOR state before flipping: the async email below fires only
        # on the PENDING → decided transition, so a re-delivery / approve replay
        # (prior state already delivered/declined) never re-sends.
        prior = await store.get_decision_by_action(action_id, workspace_id=blob_workspace)
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

        # The widget is resolved ONCE here and reused by both blocks below. It
        # was previously loaded inside the email block; the ledger emit needs the
        # same object (for the agent binding and, load-bearingly, for the real
        # ``workspace_id`` that routes the ledger FILE), and reading the same row
        # twice on an approve click would be waste. Its own guard, so a widget
        # that has since been deleted degrades to "no widget" for both consumers
        # instead of breaking the delivery that already landed.
        widget: Any = None
        try:
            widget = await store.get_widget(updated.widget_id)
        except Exception:  # noqa: BLE001 — the decision is already delivered
            logger.debug("widget lookup failed for %s", updated.widget_id, exc_info=True)

        # AL-2 — the delivered beat. Only on the DELIVERED path: this kind means
        # "the approved answer reached the person waiting for it", and a decline
        # is already counted by AL-1's ``paw.action.rejected`` row, so emitting
        # here too would put one refusal in the funnel twice. A delivery whose
        # widget no longer resolves records NOTHING rather than guessing a file
        # to route it into — the visitor still got their answer, and a row in the
        # wrong tenant's ledger is worse than an absent one. Fail-soft by
        # construction (see paw_bar/ledger.py) — never raises into the approve.
        if state == DecisionState.DELIVERED and widget is not None:
            from pocketpaw_ee.paw_bar import ledger

            await ledger.emit_action_delivered(
                action=action,
                widget=widget,
                customer_ref=updated.customer_ref,
                # In-row scope = the blob's workspace, i.e. EXACTLY the scope the
                # Instinct Action carries, so the delivered row lands in the same
                # bucket as its own approved row and AL-4 can compare the two.
                row_workspace_id=blob_workspace or "",
                decided_by=decided_by,
            )

        # Async half of the loop: the visitor left the page and parked an email
        # on the pending row (POST /paw-bar/decision-contact). Send them the
        # SAME customer-facing reply the poll returns — approved or declined —
        # exactly once (guarded on the state transition above). Best-effort:
        # the mailer is fail-soft and this whole block must never break the
        # approve/reject response.
        if prior is not None and prior.state == DecisionState.PENDING and updated.contact_email:
            try:
                from pocketpaw_ee.paw_bar import mailer

                site_name = str(getattr(widget, "name", "") or "") or "the site"
                await mailer.send_decision_email(
                    updated.contact_email,
                    f"Update from {site_name}",
                    updated.reply,
                )
            except Exception:  # noqa: BLE001 — mail is best-effort over delivery
                logger.warning(
                    "async decision email failed for action %s — the decision "
                    "is still readable on the poll endpoint",
                    action_id,
                    exc_info=True,
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
    "propose_customer_action",
    "propose_customer_decision",
    "resolve_widget_agent",
    "resolve_workspace_id",
]
