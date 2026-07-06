# Product events → notification dispatch (pocketpaw#1393).
# Created: 2026-06-09 (feat/push-wire-events) — subscribes the v1 notification
# event set to the realtime in-process bus and routes each to ``notify(...)``
# (push/dispatch.py), which forks WS-vs-Web-Push so a user is never
# double-notified. Mirrors the meeting-notifications bridge pattern
# (meetings/bridges/notifications.py): one handler per event type, each
# resolving recipients + a small {title, body, url?} payload, registered from
# ``mount_cloud`` after ``init_realtime`` installs the singleton bus.
#
# Updated: 2026-07-02 (feat/push-user-message-notifications) — wired
# ``message.sent`` → ``on_new_message`` so a human-to-human message notifies the
# group's OTHER human members. The body is generic-with-a-count ("N new
# messages") — the message text never reaches the OS notification surface
# (lock-screen privacy). Only the human ``message_add`` path emits
# ``message.sent``; agent replies ride ``agent.stream_end`` (on_agent_complete),
# so there is no double-notify.
#
# v1 events wired (those with a real emission point + a resolvable recipient):
#   - agent.stream_end           → agent-complete. Recipients = the group's
#                                  human members (resolved off the group the
#                                  payload names; the payload itself carries
#                                  no workspace_id, so we read it from the
#                                  group domain).
#   - instinct.approval.created  → guardian-block. The Instinct/guardian layer
#                                  gated an action into the approval queue;
#                                  recipient = ``requested_by`` (the user whose
#                                  action was blocked), workspace = the event's
#                                  ``workspace_id``.
#   - meeting.started            → meeting-start. Recipients = creator
#                                  (recall) or every group member (livekit),
#                                  matching the existing meeting bridge's
#                                  recipient logic.
#
# Each handler is best-effort: a failure to resolve recipients or dispatch is
# logged and swallowed so one bad event can't break the bus fan-out (the bus
# already isolates handler exceptions, but we double-guard the DB/dispatch
# calls). No Beanie writes happen here — the only writer remains
# push/service.py via dispatch.notify → send_to_user.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud._core.realtime.events import Event
from pocketpaw_ee.cloud.push import coalesce, dispatch

logger = logging.getLogger(__name__)


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
# Handlers — one per event type
# ---------------------------------------------------------------------------


async def on_agent_complete(event: Event) -> None:
    """agent.stream_end → notify the group's human members the agent replied."""
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


async def on_guardian_block(event: Event) -> None:
    """instinct.approval.created → notify the user whose action was gated."""
    data = event.data or {}
    workspace_id = data.get("workspace_id")
    recipient = data.get("requested_by")
    if not (workspace_id and recipient):
        return

    action_name = data.get("action_name") or "An action"
    payload = {
        "title": "Action needs approval",
        "body": f"{action_name} was held for your review.",
        "url": "/?view=approvals",
    }
    await _dispatch(workspace_id, recipient, payload)


async def on_meeting_started(event: Event) -> None:
    """meeting.started → notify the meeting's recipients it has begun."""
    data = event.data or {}
    workspace_id = data.get("workspace_id")
    meeting_id = data.get("meeting_id")
    if not (workspace_id and meeting_id):
        return

    source = data.get("source", "recall")
    group_id = data.get("group_id")
    if source == "livekit" and group_id:
        recipients = await _group_member_ids(group_id)
    else:
        creator = data.get("created_by") or data.get("organizer_user_id")
        recipients = [creator] if creator else []

    if not recipients:
        return

    payload = {
        "title": "Meeting started",
        "body": "A meeting has started.",
        "url": f"/?join=meeting-{meeting_id}",
    }
    for recipient in recipients:
        await _dispatch(workspace_id, recipient, payload)


async def on_new_message(event: Event) -> None:
    """message.sent → notify a group's OTHER human members of a new message.

    Generic-with-a-count: the title names the sender, the body is only the
    unread count ("N new messages") — the message text never reaches the OS
    notification surface. The sender is excluded (no self-ping), and the
    WS-vs-Web-Push dedupe in ``dispatch.notify`` means only members with no
    live connection actually receive a Web Push.

    Only the human ``message_add`` path emits ``message.sent``; the
    ``senderType != "user"`` guard is defensive belt-and-braces so an
    agent-authored message can never double-fire alongside
    ``on_agent_complete`` (agent.stream_end).

    ``message.sent`` carries the wire dict (camelCase ``senderName`` /
    ``senderType``) plus ``group_id`` + ``sender_id`` but NO ``workspace_id``,
    so the workspace is resolved from the group — mirroring on_agent_complete.

    Each send is routed through the leading-edge coalescer (push/coalesce.py):
    the first message pings instantly, a burst within the cooldown collapses to
    one trailing send. The emit closure recomputes the unread count at send
    time, so both the leading and any trailing push carry a current count.
    """
    data = event.data or {}
    group_id = data.get("group_id")
    if not group_id:
        return
    if data.get("senderType") not in (None, "user"):
        return

    workspace_id = await _group_workspace_id(group_id)
    if not workspace_id:
        return

    sender_id = data.get("sender_id")
    recipients = [uid for uid in await _group_member_ids(group_id) if uid != sender_id]
    if not recipients:
        return

    title = data.get("senderName") or "New message"
    url = f"/?join={group_id}"
    for recipient in recipients:
        # ``recipient`` is bound as a default arg so the closure captures THIS
        # iteration's value (the other captured names are constant per call).
        # The count is recomputed inside so a coalesced trailing send reflects
        # the accumulated unread total. ``tag`` is the group id so the OS
        # collapses a conversation's notifications into one updating toast.
        async def _emit(recipient: str = recipient) -> None:
            count = await _unread_count(recipient, group_id)
            await _dispatch(
                workspace_id,
                recipient,
                {
                    "title": title,
                    "body": _count_body(count),
                    "url": url,
                    "tag": group_id,
                },
            )

        await coalesce.submit((workspace_id, recipient, group_id), _emit)


# ---------------------------------------------------------------------------
# Registration — called from mount_cloud() after init_realtime.
# ---------------------------------------------------------------------------


def register_push_event_listeners() -> None:
    """Wire the v1 product events → notification dispatch. Idempotent-safe.

    Subscribes on the realtime in-process bus (the same bus the WS fan-out
    rides), so handlers fire whether or not any client is currently
    subscribed. Must run AFTER ``init_realtime`` installs the singleton bus.
    """
    from pocketpaw_ee.cloud._core.realtime.bus import get_bus

    bus = get_bus()
    bus.subscribe("agent.stream_end", on_agent_complete)
    bus.subscribe("instinct.approval.created", on_guardian_block)
    bus.subscribe("meeting.started", on_meeting_started)
    bus.subscribe("message.sent", on_new_message)
    logger.info("registered v1 product events → push notification dispatch")


__all__ = [
    "on_agent_complete",
    "on_guardian_block",
    "on_meeting_started",
    "on_new_message",
    "register_push_event_listeners",
]
