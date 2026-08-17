# ee/paw_bar/notify.py — owner notifications for the concierge inbox (slice 3).
# Created: 2026-07-31 (owner inbox, slice 3) — an inbox nobody is told about is a
#   page you have to remember to visit. This module is the one place a visitor
#   conversation becomes a notification, on exactly three triggers:
#     * ``paw_bar_conversation_new``  — the first turn of a NEW conversation.
#     * ``paw_bar_needs_human``       — a raised handoff (see ``handoff.py``).
#     * ``paw_bar_visitor_reply``     — a visitor wrote while the bot was muted,
#                                       i.e. straight at the person holding it.
#   Deliberately NOT on every turn: a chatty bar would train the owner to ignore
#   the badge, which costs them the two notifications that matter.
#
#   v1 fan-out is the WORKSPACE OWNER ALONE (design §10 Q4), matching the
#   solo-owner posture the whole inbox assumes; multi-member routing lands with
#   assignment, not before. The existing notifications module is user-room-keyed,
#   so "the workspace owner" is resolved to a user id here and handed to
#   ``notifications_service.create`` like any other recipient — no new delivery
#   path, so Slack/webhook fan-out and the unread badge come for free.
#
#   EVERY function here is fail-soft and never raises. A visitor's turn must not
#   depend on the owner's bookkeeping succeeding: the worst acceptable outcome of
#   a broken notifier is an owner who finds the conversation on their next visit
#   to the inbox, and the worst UNacceptable one is a visitor whose message 500s
#   because a Mongo read failed.

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

        await notifications_service.create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind=kind,
            title=title,
            body=safe_preview(body),
            source=NotificationSource(
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
            ),
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


__all__ = [
    "NOTIFY_NEEDS_HUMAN",
    "NOTIFY_NEW_CONVERSATION",
    "NOTIFY_SOURCE_TYPE",
    "NOTIFY_VISITOR_REPLY",
    "notify_workspace_owner",
    "resolve_widget_agent",
    "resolve_workspace_owner",
    "safe_preview",
]
