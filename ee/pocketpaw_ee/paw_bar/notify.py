# ee/paw_bar/notify.py — owner notifications for the concierge inbox.
# The one place a visitor conversation becomes a notification, on exactly three
# triggers (never on every turn, or the owner learns to ignore the badge):
#   * ``paw_bar_conversation_new``  — the first turn of a NEW conversation.
#   * ``paw_bar_needs_human``       — a raised handoff (see ``handoff.py``).
#   * ``paw_bar_visitor_reply``     — a visitor wrote while the bot was muted.
# Plus one site-level kind, ``paw_bar_spend_cap``: the v2 concierge hit the
# site's daily spend cap. At most once per site per UTC day
# (``notify_spend_cap_reached``), however many visitors land on the cap.
#
# Fan-out is the WORKSPACE OWNER (design §10 Q4). The new-conversation and
# visitor-reply kinds go through ``notifications_service.create`` (bell, push,
# and the workspace Slack/webhook). A HANDOFF is a site event: it is routed by
# the site's own notification settings (``leads.notification_settings``, event
# ``handoff``) so it can also email the site's recipients and hit the site's
# signed webhook, with the workspace config as the fallback. The site is found
# from the widget's pocket.
#
# EVERY function here is fail-soft and never raises. A visitor's turn must not
# depend on the owner's bookkeeping: the worst acceptable outcome of a broken
# notifier is an owner who finds the conversation on their next inbox visit;
# the unacceptable one is a visitor whose message 500s because a read failed.

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# The three notification kinds. Prefixed so a client can route/filter them as one
# family without matching on the title text.
NOTIFY_NEW_CONVERSATION = "paw_bar_conversation_new"
NOTIFY_NEEDS_HUMAN = "paw_bar_needs_human"
NOTIFY_VISITOR_REPLY = "paw_bar_visitor_reply"
NOTIFY_SPEND_CAP = "paw_bar_spend_cap"
# Its source: ``id`` = "<pocket_id>:<YYYY-MM-DD>", the site and the UTC day, which
# is also the dedupe key.
NOTIFY_SPEND_CAP_SOURCE_TYPE = "paw_bar_site"

# The notification source ``type``, with ``id`` = "<widget_id>:<customer_ref>" —
# the exact pair the owner inbox is keyed by, so a click can resolve the thread.
# A compound id rather than borrowing ``pocket_id``/``room_id`` for something
# they don't mean. The source ALSO carries ``agent_id``: the compound id names
# the conversation, but a client still needs somewhere to open it, and the
# concierge inbox lives on an agent rather than in a chat room.
NOTIFY_SOURCE_TYPE = "paw_bar_conversation"

# How much of a visitor's line rides along as the notification body. Enough to
# decide whether to open it now; never the whole message (the thread has that).
_MAX_BODY_CHARS = 160

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")


def safe_preview(text: Any, cap: int = _MAX_BODY_CHARS) -> str:
    """Sanitize visitor-typed text for an owner-facing surface.

    Strips control characters, collapses whitespace, and caps the length —
    the same defense-in-depth the decision loop applies before a visitor's words
    appear in a proposal a human reads. The frontend still escapes on render;
    this keeps the stored notification from carrying terminal control sequences
    or a wall of text in the first place.
    """
    raw = str(text or "")
    return _WHITESPACE_RE.sub(" ", _CONTROL_CHARS_RE.sub("", raw)).strip()[:cap]


async def resolve_workspace_owner(workspace_id: str) -> str:
    """The user id to notify for a workspace, or "" when there isn't one.

    ``Workspace.owner`` is the singular owner field (the admin who created the
    tenant) — v1's whole fan-out. Returns "" rather than raising for every reason
    it can fail: a blank workspace, an id that is not an ObjectId (a legacy or
    test-shaped tenant handle), a workspace that no longer exists, or an
    unavailable database. Every one of those means "nobody to notify", which is a
    normal answer on this path, not an error.
    """
    if not workspace_id:
        return ""
    try:
        from beanie import PydanticObjectId

        from pocketpaw_ee.cloud.models.workspace import Workspace

        try:
            oid = PydanticObjectId(workspace_id)
        except Exception:  # noqa: BLE001 — a non-ObjectId handle simply has no doc
            return ""
        doc = await Workspace.get(oid)
        return str(getattr(doc, "owner", "") or "") if doc is not None else ""
    except Exception:  # noqa: BLE001 — notification routing is never load-bearing
        logger.debug("workspace owner lookup failed for %s", workspace_id, exc_info=True)
        return ""


async def resolve_widget_agent(widget_id: str, workspace_id: str = "") -> str:
    """The concierge agent bound to a widget, or "" when there isn't one.

    Resolved HERE rather than at each call site on purpose. The three producers
    differ in what they hold — the new-conversation path has the widget object,
    the handoff path only has its id — and a notification that silently omits the
    agent is not a visible failure: it degrades to a link the owner can't follow,
    which is precisely the bug this field exists to fix. One resolver means a
    fourth producer cannot forget. Workspace-scoped like every other widget read.

    Returns "" for every failure mode (no widget, unbound widget, store error),
    all of which mean "no agent to link to" — a normal answer on this path.
    """
    if not widget_id:
        return ""
    try:
        from pocketpaw.stores import get_paw_bar_store

        store = get_paw_bar_store(workspace_id=workspace_id or None)
        widget = await store.get_widget(widget_id, workspace_id=workspace_id or None)
        return str(getattr(widget, "agent_id", "") or "") if widget is not None else ""
    except Exception:  # noqa: BLE001 — notification routing is never load-bearing
        logger.debug("widget agent lookup failed for %s", widget_id, exc_info=True)
        return ""


async def resolve_widget_site(widget_id: str, workspace_id: str) -> tuple[str, str]:
    """(site id, site name) of the site whose pocket carries this widget, or
    ("", "") when there is none. Workspace-scoped; never raises."""
    if not widget_id or not workspace_id:
        return "", ""
    try:
        from pocketpaw.stores import get_paw_bar_store
        from pocketpaw_ee.cloud.models.site import Site

        store = get_paw_bar_store(workspace_id=workspace_id)
        widget = await store.get_widget(widget_id, workspace_id=workspace_id)
        pocket_id = str(getattr(widget, "pocket_id", "") or "") if widget is not None else ""
        if not pocket_id:
            return "", ""
        site = await Site.find_one({"workspace": workspace_id, "pocket_id": pocket_id})
        return (str(site.id), site.name or "") if site is not None else ("", "")
    except Exception:  # noqa: BLE001 — notification routing is never load-bearing
        logger.debug("widget site lookup failed for %s", widget_id, exc_info=True)
        return "", ""


async def notify_workspace_owner(
    *,
    workspace_id: str,
    kind: str,
    title: str,
    body: str = "",
    widget_id: str = "",
    customer_ref: str = "",
    agent_id: str = "",
) -> bool:
    """Notify the workspace owner about one conversation. Never raises.

    Returns whether a notification was created — ``False`` covers "no owner
    resolved" and "the notification service failed" alike, because neither is
    something a caller on a visitor's hot path can or should do anything about.
    """
    try:
        recipient = await resolve_workspace_owner(workspace_id)
        if not recipient:
            return False

        # Caller-supplied wins (it already holds the widget — no second read);
        # otherwise resolve it, so no producer can omit it by forgetting.
        agent_id = agent_id or await resolve_widget_agent(widget_id, workspace_id)

        from pocketpaw_ee.cloud.notifications import service as notifications_service
        from pocketpaw_ee.cloud.notifications.domain import NotificationSource

        source = NotificationSource(
            type=NOTIFY_SOURCE_TYPE,
            id=f"{widget_id}:{customer_ref}",
            # The bound concierge agent. The compound id above says WHICH
            # conversation; this says where that conversation can be opened.
            # Without it a client has no id to build a link from and falls
            # back to the chat surface — which is exactly what happened:
            # the click landed on /chat/<widget_id>:<customer_ref>, a room
            # that cannot exist, and the empty default agent rendered.
            # "" (an unbound or legacy widget) stays None, and the client
            # degrades to the agents list rather than a dead room.
            agent_id=agent_id or None,
        )
        if kind == NOTIFY_NEEDS_HUMAN:
            site_id, site_name = await resolve_widget_site(widget_id, workspace_id)
            if site_id:
                from pocketpaw_ee.cloud.leads import notification_settings
                from pocketpaw_ee.cloud.notifications.email import app_base_url

                inbox = f"/agents/{agent_id}?tab=conversations" if agent_id else "/agents"
                await notification_settings.dispatch_site_event(
                    workspace_id=workspace_id,
                    site_ref=site_id,
                    event="handoff",
                    kind=kind,
                    title=title,
                    body=safe_preview(body),
                    source=source,
                    push_recipients=[recipient],
                    event_data={
                        "site_id": site_id,
                        "site_name": site_name,
                        "widget_id": widget_id,
                        "customer_ref": customer_ref,
                        "agent_id": agent_id or None,
                        "question": safe_preview(body, cap=2000),
                    },
                    link=f"{app_base_url()}{inbox}",
                )
                return True
        await notifications_service.create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind=kind,
            title=title,
            body=safe_preview(body),
            source=source,
        )
        return True
    except Exception:  # noqa: BLE001 — a visitor's turn never fails on this
        logger.warning(
            "paw-bar owner notification failed (kind=%s, widget=%s)",
            kind,
            widget_id,
            exc_info=True,
        )
        return False


# (workspace, source id) pairs this process already notified or found notified,
# so a capped site costs one Mongo read per process per day, not one per turn.
_spend_cap_noted: set[tuple[str, str]] = set()


async def notify_spend_cap_reached(
    *, workspace_id: str, pocket_id: str, site_name: str = "", widget_id: str = ""
) -> bool:
    """Tell the workspace owner the site's concierge hit today's spend cap, once
    per site per UTC day. Never raises; returns whether a notification was created.

    The dedupe is today's notification row itself (same kind, recipient and source
    id), checked before writing and remembered per process. Two workers reaching
    the cap in the same instant can each write one; that is the whole race."""
    from datetime import UTC, datetime

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    source_id = f"{pocket_id}:{day}"
    key = (workspace_id, source_id)
    if key in _spend_cap_noted:
        return False
    try:
        recipient = await resolve_workspace_owner(workspace_id)
        if not recipient:
            return False

        from pocketpaw_ee.cloud.models.notification import Notification as NotificationDoc
        from pocketpaw_ee.cloud.notifications import service as notifications_service
        from pocketpaw_ee.cloud.notifications.domain import NotificationSource

        existing = await NotificationDoc.find_one(
            {
                "workspace": workspace_id,
                "recipient": recipient,
                "type": NOTIFY_SPEND_CAP,
                "source.id": source_id,
            }
        )
        _spend_cap_noted.add(key)
        if existing is not None:
            return False
        name = safe_preview(site_name, cap=80)
        await notifications_service.create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind=NOTIFY_SPEND_CAP,
            title=(
                f"{name}: the concierge reached today's spend cap"
                if name
                else "Your site's concierge reached today's spend cap"
            ),
            body=(
                "Visitors are told the assistant is unavailable until the cap resets "
                "at midnight UTC. Raise the cap to keep answering today."
            ),
            source=NotificationSource(
                type=NOTIFY_SPEND_CAP_SOURCE_TYPE,
                id=source_id,
                pocket_id=pocket_id or None,
            ),
        )
        return True
    except Exception:  # noqa: BLE001 — a visitor's turn never fails on this
        logger.warning(
            "paw-bar spend-cap notification failed (widget=%s)", widget_id, exc_info=True
        )
        return False


__all__ = [
    "NOTIFY_NEEDS_HUMAN",
    "NOTIFY_NEW_CONVERSATION",
    "NOTIFY_SOURCE_TYPE",
    "NOTIFY_SPEND_CAP",
    "NOTIFY_SPEND_CAP_SOURCE_TYPE",
    "NOTIFY_VISITOR_REPLY",
    "notify_spend_cap_reached",
    "notify_workspace_owner",
    "resolve_widget_agent",
    "resolve_widget_site",
    "resolve_workspace_owner",
    "safe_preview",
]
