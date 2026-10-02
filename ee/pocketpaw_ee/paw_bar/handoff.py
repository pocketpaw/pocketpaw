# ee/paw_bar/handoff.py — the human-handoff PRODUCER (owner inbox).
#
# The ONLY writer of handoffs: the visitor's "talk to a person" button
# (POST /paw-bar/request-human), the legacy agent's ``pawbar_request_human`` tool
# and the v2 runner's degrade path all land in ``raise_handoff``, so they can't
# drift about what a handoff is. In order, each step fail-soft:
#   1. escalate the conversation to ``needs_human`` (the state the inbox filters on);
#   2. write the ``_paw_handoffs`` Fabric object the handoffs read consumes:
#      {widget_id, contact, question, transcript_ref = customer_ref};
#   3. when the visitor left a usable email or phone, write ONE Lead for the
#      conversation (form_type and source.kind "handoff", the question as
#      ``message``, conversation_ref "<widget_id>:<customer_ref>"), so every way a
#      visitor leaves contact details lands on the site's Leads page. A second
#      handoff in the same conversation adds no second lead (a partial unique
#      index on the Lead decides, so concurrent handoffs can't both insert). Its
#      ``lead.captured`` carries source_kind "handoff", which the leads bridge
#      skips: step 6 already tells the owner;
#   4. record the rate-limit / audit marker;
#   5. record ``paw.handoff.raised`` in the agent ledger, keyed on the Fabric id
#      (``paw.handoff.resolved`` fires where a thread leaves needs_human, in the
#      inbox PATCH, via ``ledger.emit_conversation_transition``);
#   6. notify the workspace owner (``notify``).
# The call succeeds when step 1 OR 2 landed (two separate owner-visible surfaces);
# only when both fail does the visitor hear 503.
#
# ZERO AUTHORITY (SS-2): this runs no declared verb and touches no catalog, cart,
# pocket or other tenant data. A concierge reaching it can escalate itself to a
# human and nothing else; every write is scoped to the widget's own workspace.

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# The reserved Fabric object type a handoff request is stored as (SS-6). Owned
# here, beside the writer, and imported by the router's reader so the producer
# and the consumer can never name two different types.
PAW_HANDOFFS_TYPE = "_paw_handoffs"

# The verb the built-in escape-hatch tool is exposed under
# (``mcp__pawbar_actions__pawbar_request_human``). It is deliberately NOT a
# spec-declared action: every site gets it, including the overwhelming majority
# that declare no actions at all.
HANDOFF_VERB = "request_human"

# The event marker type a raised handoff records. Its own type (not the generic
# action marker) so the dedicated cap below counts handoffs alone, exactly as
# ``GATED_MARKER_TYPE`` does for proposal-generating actions.
HANDOFF_MARKER_TYPE = "pawbar_handoff"

# How many handoffs one visitor may raise per minute. Low on purpose: a handoff
# writes a durable record AND pings the owner, so the abuse shape here is
# notification spam rather than compute. Three leaves room for a visitor who
# asks twice because the first attempt didn't feel acknowledged.
HANDOFFS_PER_MIN = 3

# Bounds on the visitor-supplied strings that end up on the stored object and in
# the owner's notification. The question is a summary for a queue row, not a
# transcript — the full conversation is one click away in the thread.
_MAX_QUESTION_CHARS = 500
# RFC-ish practical maximum, matching the decision-contact capture's cap.
_MAX_CONTACT_CHARS = 254

# Control characters (except tab/newline, which collapse to spaces below) are
# stripped from anything a visitor typed before it reaches an owner-facing
# surface — same defense-in-depth the decision loop applies to proposals.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class HandoffOutcome:
    """The producer's structured result — mapped to HTTP or MCP by the caller.

    ``ok`` says a human will see this conversation. ``handoff_id`` is the Fabric
    object id when one was written (empty when only the queue flip landed, which
    is still a success — see the module header). ``error`` is a stable machine
    code and ``http_status`` the status the public endpoint should return.
    """

    ok: bool
    handoff_id: str = ""
    escalated: bool = False
    error: str = ""
    http_status: int = 200


def _clean(text: Any, cap: int) -> str:
    """Strip control chars, collapse whitespace, cap — for visitor-typed text."""
    raw = str(text or "")
    return _WHITESPACE_RE.sub(" ", _CONTROL_CHARS_RE.sub("", raw)).strip()[:cap]


async def _within_handoff_rate(store: Any, widget_id: str, customer_ref: str) -> bool:
    """Dedicated per-visitor cap on handoffs. Best-effort → allow on error.

    Counted off the same event log the widget's overall limiter uses, filtered to
    ``HANDOFF_MARKER_TYPE``, so a visitor rotating nothing but their handoff
    button can't outrun the owner's notifications. A counting failure ALLOWS the
    handoff: refusing someone's request to reach a person because a rate query
    hiccuped is the wrong way to fail.
    """
    try:
        window = datetime.now() - timedelta(minutes=1)
        recent = await store.count_events_since(
            widget_id, window, customer_ref=customer_ref, event_type=HANDOFF_MARKER_TYPE
        )
        return recent < HANDOFFS_PER_MIN
    except Exception:  # noqa: BLE001 — a broken counter must not block an escalation
        logger.debug("handoff rate check failed (allowing)", exc_info=True)
        return True


async def _escalate_conversation(
    store: Any, widget_id: str, customer_ref: str, workspace_id: str, contact: str
) -> bool:
    """Flip the conversation to ``needs_human``. Returns whether it landed.

    ``ensure_conversation`` first, because a handoff can be the FIRST thing that
    ever happens on a legacy conversation (one that predates the state table) and
    ``update_conversation`` writes nothing when there is no row. It is the
    create-without-touch variant on purpose: raising a handoff is not new visitor
    activity, so it must not bump the unread counter twice for one message.

    ``contact`` is promoted onto the row when the visitor supplied one here. That
    is a deliberate, first-party disclosure made FOR this purpose, not a copy of
    the decision row's captured email (whose PII invariant keeps it where it was
    left) — so the owner's list can call this person by name while the invariant
    holds.
    """
    try:
        await store.ensure_conversation(widget_id, customer_ref, workspace_id)
        fields: dict[str, Any] = {"state": "needs_human"}
        if contact:
            fields["contact_email"] = contact
        updated = await store.update_conversation(
            widget_id, customer_ref, workspace_id=workspace_id, **fields
        )
        return updated is not None
    except Exception:  # noqa: BLE001 — the Fabric record can still carry the handoff
        logger.warning(
            "handoff conversation escalation failed for widget %s (non-fatal)",
            widget_id,
            exc_info=True,
        )
        return False


async def _write_handoff_object(
    widget_id: str, workspace_id: str, properties: dict[str, Any]
) -> str:
    """Write the ``_paw_handoffs`` Fabric object the owner read consumes.

    Scoped to the widget's workspace on BOTH halves: the type is resolved (and
    defined, first time) for that workspace, and the object row is stamped with
    it — the same tenancy seam ``_query_handoff_objects`` reads back through. The
    type carries an EMPTY property schema so the declared-only validator accepts
    the four contract fields without a migration if the shape ever grows.

    Returns the new object's id, or "" when the write could not be made.
    """
    try:
        from pocketpaw_ee.api import get_fabric_store

        fabric = get_fabric_store(workspace_id=workspace_id or None)
        if fabric is None:
            return ""
        obj_type = await fabric.get_type_by_name(PAW_HANDOFFS_TYPE, workspace_id=workspace_id)
        if obj_type is None:
            obj_type = await fabric.define_type(
                PAW_HANDOFFS_TYPE,
                properties=[],
                description="A visitor's request to talk to a person (Paw Bar concierge).",
                workspace_id=workspace_id or None,
            )
        created = await fabric.create_object(
            type_id=obj_type.id,
            properties=properties,
            source_connector="paw_bar",
            source_id=widget_id,
            workspace_id=workspace_id or None,
        )
        return str(getattr(created, "id", "") or "")
    except Exception:  # noqa: BLE001 — the queue flip alone still reaches the owner
        logger.warning("handoff object write failed for widget %s", widget_id, exc_info=True)
        return ""


async def _record_marker(store: Any, widget_id: str, customer_ref: str, source: str) -> None:
    """Best-effort audit + rate-limit marker, mirroring ``actions._record_action_marker``."""
    try:
        from pocketpaw.paw_bar.models import PawBarEvent

        await store.record_event(
            PawBarEvent(
                widget_id=widget_id,
                type=HANDOFF_MARKER_TYPE,
                payload={"source": source},
                customer_ref=customer_ref,
            )
        )
    except Exception:  # noqa: BLE001
        logger.debug("handoff marker record failed (non-fatal)", exc_info=True)


async def _write_handoff_lead(
    widget: Any, workspace_id: str, customer_ref: str, question: str, contact: str
) -> None:
    """Step 3 of the module header. Never raises: the handoff already landed."""
    from pocketpaw.sites_capture.contact_form import looks_like_email, looks_like_phone

    if looks_like_email(contact):
        properties = {"email": contact}
    elif looks_like_phone(contact):
        properties = {"phone": contact}
    else:
        return
    if question:
        properties["message"] = question
    widget_id = str(getattr(widget, "id", "") or "")
    pocket_id = str(getattr(widget, "pocket_id", "") or "")
    if not (widget_id and pocket_id and workspace_id):
        return
    try:
        from pocketpaw_ee.cloud.leads import service as leads_service
        from pocketpaw_ee.cloud.models.site import Site
        from pocketpaw_ee.paw_bar.ledger import conversation_id

        site = await Site.find_one({"workspace": workspace_id, "pocket_id": pocket_id})
        if site is None:
            return
        ref = conversation_id(widget_id, customer_ref)
        if await leads_service.has_conversation_lead(
            workspace_id, site.script_name, "handoff", ref
        ):
            return
        await leads_service.capture_internal(
            site=site,
            form_type="handoff",
            kind="handoff",
            properties=properties,
            conversation_ref=ref,
        )
    except Exception:  # noqa: BLE001 — a lead is bookkeeping; the handoff stands
        logger.warning("handoff lead write failed for widget %s", widget_id, exc_info=True)


async def raise_handoff(
    *,
    widget: Any,
    workspace_id: str,
    customer_ref: str,
    question: str = "",
    contact: str = "",
    source: str = "visitor",
    store: Any | None = None,
) -> HandoffOutcome:
    """Raise a human-handoff for one visitor conversation — the single producer.

    ``widget`` is the resolved :class:`PawBarWidget` (already bound to the
    caller's tenant by the front gate or by the tool's workspace-scoped reload);
    ``workspace_id`` is the authenticated tenant every write below is scoped to;
    ``question`` and ``contact`` are visitor-typed and are sanitized here.
    ``source`` records WHO raised it ("visitor" for the always-available button,
    "agent" for the concierge tool) on the audit marker.

    Never raises. See the module header for the ordering and the partial-failure
    contract.
    """
    if store is None:
        from pocketpaw.stores import get_paw_bar_store

        store = get_paw_bar_store()

    widget_id = str(getattr(widget, "id", "") or "")
    if not widget_id:
        return HandoffOutcome(ok=False, error="widget_unresolved", http_status=409)
    if not customer_ref:
        return HandoffOutcome(ok=False, error="invalid_customer_ref", http_status=400)

    if not await _within_handoff_rate(store, widget_id, customer_ref):
        return HandoffOutcome(ok=False, error="handoff_rate_limit", http_status=429)

    question_text = _clean(question, _MAX_QUESTION_CHARS)
    contact_text = _clean(contact, _MAX_CONTACT_CHARS)

    escalated = await _escalate_conversation(
        store, widget_id, customer_ref, workspace_id, contact_text
    )
    handoff_id = await _write_handoff_object(
        widget_id,
        workspace_id,
        {
            "widget_id": widget_id,
            "contact": contact_text,
            "question": question_text,
            # The transcript endpoint is keyed by customer_ref, so this is a
            # working pointer into the thread rather than a decorative id.
            "transcript_ref": customer_ref,
        },
    )
    await _record_marker(store, widget_id, customer_ref, source)

    if not escalated and not handoff_id:
        # Both owner-visible surfaces failed — say so rather than telling a
        # visitor a person is coming when nothing recorded that they asked.
        return HandoffOutcome(ok=False, error="handoff_unavailable", http_status=503)

    await _write_handoff_lead(widget, workspace_id, customer_ref, question_text, contact_text)

    logger.info(
        "paw_bar.handoff.raised widget=%s source=%s escalated=%s object=%s",
        widget_id,
        source,
        escalated,
        handoff_id or "-",
    )

    # AL-2 — the handoff RAISED beat, recorded only once we know a human will
    # actually see this (the both-surfaces-failed arm returned above). Fail-soft
    # by construction; a visitor's escape hatch never depends on bookkeeping.
    from pocketpaw_ee.paw_bar import ledger

    await ledger.emit_handoff_raised(
        widget=widget,
        # The authenticated tenant — the SAME value ``_write_handoff_object``
        # routes the Fabric store by. It is a store-path-safe token, unlike the
        # widget owner label, so it can route the ledger file too.
        workspace_id=workspace_id,
        customer_ref=customer_ref,
        handoff_id=handoff_id,
        source=source,
    )

    from pocketpaw_ee.paw_bar.notify import NOTIFY_NEEDS_HUMAN, notify_workspace_owner

    await notify_workspace_owner(
        workspace_id=workspace_id,
        kind=NOTIFY_NEEDS_HUMAN,
        title="A visitor asked for a person",
        body=question_text,
        widget_id=widget_id,
        customer_ref=customer_ref,
    )
    return HandoffOutcome(ok=True, handoff_id=handoff_id, escalated=escalated)


__all__ = [
    "HANDOFFS_PER_MIN",
    "HANDOFF_MARKER_TYPE",
    "HANDOFF_VERB",
    "PAW_HANDOFFS_TYPE",
    "HandoffOutcome",
    "raise_handoff",
]
