# Persisted notifications → push dispatch (pocketpaw#1393).
# Created: 2026-06-09 (feat/push-wire-events) — subscribed a hand-picked set of
# product events to the realtime in-process bus and routed each to
# ``notify(...)`` (push/dispatch.py), which forks WS-vs-Web-Push so a user is
# never double-notified.
#
# Updated: 2026-07-02 (feat/push-user-message-notifications) — added a
# ``message.sent`` handler so human-to-human messages notified the group's
# other members, throttled through push/coalesce.py.
#
# Updated: 2026-08-11 (fix/notif-push-convergence) — CONVERGED on the persisted
# notification. The old design wired FOUR product events by hand while
# ``notifications/service.create`` produces a dozen kinds, so invites,
# pocket-shares, task assignments, captured leads, meeting reminders and every
# concierge kind reached the in-app bell but NEVER reached the OS. Each handler
# also re-resolved its own recipients, so push drifted from the bell by
# construction. Now ONE handler subscribes to ``notification.new`` — the event
# ``service.create`` emits for every persisted notification — and the recipient,
# workspace and title come straight off the wire DTO. Bell and OS can no longer
# disagree: if a row was written, a push was attempted.
#
# Events wired today:
#   - notification.new  → on_notification_new. THE convergence handler. One
#                         push per persisted notification, recipient = the
#                         DTO's ``user_id``, workspace = its ``workspace_id``.
#   - agent.stream_end  → on_agent_complete. KEPT deliberately. Agent replies
#                         are the one product event with NO notification row:
#                         ``chat/message_service.create_agent_message`` persists
#                         the message but never calls
#                         ``notifications_service.create``. Retiring this
#                         handler would silently drop agent-reply push. It
#                         cannot double-fire — no row means no
#                         ``notification.new`` for the same reply.
#
# Retired here (their flows persist a notification, so ``notification.new``
# now carries them):
#   - message.sent               → ``message_service.add_message`` creates a
#                                  ``message`` row for EVERY non-self member,
#                                  not only mentions, so the old per-message
#                                  fan-out is fully covered.
#   - meeting.started            → ``meetings/bridges/notifications.py`` creates
#                                  a ``meeting_started`` row with byte-identical
#                                  recipient logic (creator for recall, group
#                                  members for livekit).
#   - instinct.approval.created  → ``instinct_approvals/service.create_approval``
#                                  now writes an ``instinct_approval`` row
#                                  (added in the same change), so the guardian
#                                  block rides the converged path.
#
# LOCK-SCREEN PRIVACY. A push body lands on a lock screen, so it must never
# carry user-authored text. Several kinds persist exactly that — ``message`` /
# ``mention`` / ``reaction`` store ``content[:200]``, the concierge kinds store
# a visitor's words, ``lead_captured`` stores lead fields. Those bodies are
# REPLACED here with a generic one (see ``_GENERIC_BODIES`` and
# ``_push_body``); ``message`` keeps the pre-convergence "N new messages" count
# UX. Only kinds whose persisted body is already generic pass through verbatim.
#
# Each handler is best-effort: a failure to resolve recipients or dispatch is
# logged and swallowed so one bad event can't break the bus fan-out (the bus
# already isolates handler exceptions, but we double-guard the DB/dispatch
# calls). No Beanie writes happen here — the only writer remains
# push/service.py via dispatch.notify → send_to_user.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud._core.realtime.events import Event, NotificationNew
from pocketpaw_ee.cloud.push import coalesce, dispatch

logger = logging.getLogger(__name__)


# Kinds whose PERSISTED body carries user-authored or otherwise sensitive text.
# The push body is replaced with the generic string here so the lock screen
# never shows it. ``message`` is absent on purpose — it gets the unread-count
# body instead (see ``_push_body``), preserving the pre-convergence UX.
_GENERIC_BODIES: dict[str, str] = {
    "mention": "You were mentioned.",
    "reaction": "Someone reacted to your message.",
    "lead_captured": "A new lead came in.",
    "paw_bar_conversation_new": "A new conversation started.",
    "paw_bar_needs_human": "A conversation needs you.",
    "paw_bar_visitor_reply": "A visitor replied.",
}


# ---------------------------------------------------------------------------
# Recipient resolution helpers
# ---------------------------------------------------------------------------


async def _group_member_ids(group_id: str) -> list[str]:
    """Human member user_ids for a group. Returns [] on any error."""
    try:
        from pocketpaw_ee.cloud.chat import group_service

        return await group_service.list_member_ids(group_id)
    except Exception:
        logger.exception("push: failed to list members for group=%s", group_id)
        return []


async def _group_workspace_id(group_id: str) -> str | None:
    """Resolve the workspace a group belongs to. None on any error/miss.

    ``agent.stream_end`` carries ``group_id`` but no ``workspace_id``; the
    Web Push leg needs the workspace to find the tenant's VAPID key, so we
    read it from the group domain here.
    """
    try:
        from pocketpaw_ee.cloud.chat import group_service

        group = await group_service.get_for_dispatch(group_id)
        return group.workspace_id if group else None
    except Exception:
        logger.exception("push: failed to resolve workspace for group=%s", group_id)
        return None


async def _dispatch(workspace_id: str, user_id: str, payload: dict[str, Any]) -> None:
    """Route one notification through the WS/Web-Push dedupe. Best-effort."""
    try:
        await dispatch.notify(workspace_id, user_id, payload)
    except Exception:
        logger.exception(
            "push: notify dispatch failed for workspace=%s user=%s",
            workspace_id,
            user_id,
        )


async def _unread_count(user_id: str, group_id: str) -> int:
    """Unread count for one (user, group), for the new-message body. Best-effort.

    Falls back to ``1`` on any error so the notification body still reads
    sensibly ("1 new message") rather than "0 new messages".
    """
    try:
        from pocketpaw_ee.cloud.chat import unread_service

        return await unread_service.unread_count(user_id, group_id)
    except Exception:
        logger.exception("push: unread count failed for user=%s group=%s", user_id, group_id)
        return 1


def _count_body(count: int) -> str:
    """Generic, content-free body carrying only the unread count.

    The message text is deliberately NOT included — a human-to-human push
    lands on the lock screen, so only the count crosses that surface.
    """
    return "1 new message" if count <= 1 else f"{count} new messages"


# ---------------------------------------------------------------------------
# Payload builders — kind → {title, body, url?, tag?}
# ---------------------------------------------------------------------------


def _target_url(data: dict[str, Any]) -> str | None:
    """Deep link for a persisted notification, or None when it has none.

    Mirrors the frontend resolver ``paw-enterprise/src/lib/core/notifications/
    target.ts`` (``targetUrl``) — that file is the source of truth; this is the
    subset that is unambiguous from the wire DTO alone. Two deliberate
    departures, both to avoid shipping a link that lands nowhere:

      * ``paw_bar_conversation`` — the frontend splits the compound
        ``<widget_id>:<customer_ref>`` id to reopen the exact conversation. We
        stop at the agent's conversations tab rather than duplicate that
        parsing, and fall back to ``/agents`` with no bound agent. (The
        frontend's old DEFAULT arm sent this to ``/chat/<widget>:<ref>``, a room
        that cannot exist — found live 2026-07-31.)
      * ``instinct_approval`` — omitted. The approvals tray is a global overlay
        opened via ``approvalsStore.openTray()``, not a route, so there is no
        URL to point at. The pre-convergence handler sent ``/?view=approvals``,
        which nothing in the frontend reads.
    """
    source_type = data.get("source_type")
    source_id = data.get("source_id")
    if not (source_type and source_id):
        return None

    room_id = data.get("source_room_id")
    pocket_id = data.get("source_pocket_id")
    nav_target = room_id or source_id

    if source_type == "instinct_approval":
        return None
    if source_type == "paw_bar_conversation":
        agent_id = data.get("source_agent_id")
        return f"/agents/{agent_id}?tab=conversations" if agent_id else "/agents"
    if source_type in ("message", "mention"):
        return f"/pockets/{pocket_id}/chat" if pocket_id else f"/chat/{nav_target}"
    if source_type == "invite":
        return f"/invite/{source_id}"
    if source_type == "pocket_shared":
        return f"/pockets/{source_id}"
    if source_type == "meeting":
        return f"/meetings?id={source_id}"
    if source_type == "meeting_started":
        return f"/chat/{nav_target}?join=meeting-{source_id}"
    # Same default arm as the frontend: unknown source types resolve to the
    # chat room, which is where every other current backend kind lives.
    return f"/chat/{nav_target}"


async def _push_body(data: dict[str, Any]) -> str:
    """The body that may cross a lock screen. Never user-authored text.

    ``message`` recomputes the unread count at send time so a coalesced
    trailing flush carries a current total (the pre-convergence behaviour).
    Every other content-bearing kind gets its generic replacement; the rest
    pass through the persisted body, which is already generic at create time.
    """
    kind = data.get("kind") or ""
    if kind == "message":
        room_id = data.get("source_room_id")
        user_id = data.get("user_id") or ""
        count = await _unread_count(user_id, room_id) if room_id else 1
        return _count_body(count)
    generic = _GENERIC_BODIES.get(kind)
    if generic is not None:
        return generic
    return data.get("body") or ""


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def on_notification_new(event: Event) -> None:
    """notification.new → push the persisted notification to its recipient.

    THE convergence handler: one push per persisted notification, for every
    kind ``notifications/service.create`` writes. The payload is built entirely
    from the wire DTO the event carries (``id, user_id, workspace_id, kind,
    title, body, source_*, read, created_at``) — no second recipient
    resolution, so push cannot drift from the bell.

    Each send is routed through the leading-edge coalescer (push/coalesce.py)
    keyed on ``(workspace, user, source_room_id or kind)``: a burst in one
    conversation — or a burst of one room-less kind — collapses to a leading
    push plus at most one trailing flush per window. Kinds that persist two
    rows for a single user action (a mention writes both ``message`` and
    ``mention``) share a room, so they share a key and collapse; the OS then
    folds them into one toast via the shared ``tag``.
    """
    data = event.data or {}
    user_id = data.get("user_id")
    workspace_id = data.get("workspace_id")
    if not (user_id and workspace_id):
        return

    kind = data.get("kind") or ""
    title = data.get("title") or "Notification"
    url = _target_url(data)
    # Collapse key for the OS: same conversation (or, room-less, same kind)
    # replaces the earlier toast instead of stacking.
    tag = data.get("source_room_id") or kind or None

    async def _emit() -> None:
        payload: dict[str, Any] = {"title": title, "body": await _push_body(data)}
        if url:
            payload["url"] = url
        if tag:
            payload["tag"] = tag
        await _dispatch(workspace_id, user_id, payload)

    await coalesce.submit((workspace_id, user_id, tag or "notification"), _emit)


async def on_agent_complete(event: Event) -> None:
    """agent.stream_end → notify the group's human members the agent replied.

    Not covered by ``on_notification_new``: ``create_agent_message`` persists
    the agent's message but writes NO notification row, so there is no
    ``notification.new`` for an agent reply — and therefore no double-fire
    either. If a future change starts persisting a row for agent replies, this
    handler must be retired in the same commit.
    """
    data = event.data or {}
    group_id = data.get("group_id")
    if not group_id:
        return

    workspace_id = await _group_workspace_id(group_id)
    if not workspace_id:
        return

    recipients = await _group_member_ids(group_id)
    if not recipients:
        return

    agent_name = data.get("agent_name") or "Your agent"
    payload = {
        "title": f"{agent_name} replied",
        "body": "Your agent finished responding.",
        "url": f"/?join={group_id}",
    }
    for recipient in recipients:
        await _dispatch(workspace_id, recipient, payload)


# ---------------------------------------------------------------------------
# Registration — called from mount_cloud() after init_realtime.
# ---------------------------------------------------------------------------


def register_push_event_listeners() -> None:
    """Wire persisted notifications + agent replies → push. Idempotent-safe.

    Subscribes on the realtime in-process bus (the same bus the WS fan-out
    rides), so handlers fire whether or not any client is currently
    subscribed. Must run AFTER ``init_realtime`` installs the singleton bus.
    """
    from pocketpaw_ee.cloud._core.realtime.bus import get_bus

    bus = get_bus()
    bus.subscribe(NotificationNew.EVENT_TYPE, on_notification_new)
    bus.subscribe("agent.stream_end", on_agent_complete)
    logger.info("registered notification.new + agent.stream_end → push dispatch")


__all__ = [
    "on_agent_complete",
    "on_notification_new",
    "register_push_event_listeners",
]
