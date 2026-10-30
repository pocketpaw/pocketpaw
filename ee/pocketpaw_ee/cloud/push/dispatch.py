# Notification dispatch + WS-vs-Web-Push dedupe (pocketpaw#1393).
#
# ``notify`` is the single dispatch product events call. It forks the transport
# so a user with BOTH the desktop app (live WebSocket) and a browser tab (Web
# Push) open is never double-notified:
#   - live WebSocket on THIS process   -> a ``notification.push`` WS frame; no
#     Web Push. If zero sockets accept it (half-open sockets), fall through to
#     Web Push in the same call (``ws_fallback_push``).
#   - live WebSocket on ANOTHER process (``POCKETPAW_REALTIME_BUS=redis-streams``,
#     answered by ``_core/realtime/presence.py``) -> the frame is relayed over the
#     broadcast stream and the owning process delivers it; no Web Push. The
#     zero-accept fallback does not cover remote sockets: the relay cannot report
#     back how many accepted.
#   - no live connection anywhere      -> Web Push (``send_to_user``).
#
# A thin orchestrator: the push service owns the Web Push leg (and the only
# Beanie writes), the chat ConnectionManager the local liveness check and WS
# leg. The chosen transport is returned on ``NotifyResult`` so the fork is
# observable and testable without real sockets.

from __future__ import annotations

import logging
from dataclasses import dataclass

from pocketpaw_ee.cloud.push import service as push_service
from pocketpaw_ee.cloud.push.dto import PushPayload, SendResult

logger = logging.getLogger(__name__)

# The WS event type the desktop/Tauri client listens for to raise a native
# notification. Distinct from the in-app ``notification.new`` bell event
# (that one is the persisted-notification fan-out); this is the lightweight
# "raise an OS/browser notification now" signal that mirrors what a Web Push
# would have shown, so a live desktop client and a backgrounded browser tab
# get the same notification through exactly one transport.
WS_NOTIFICATION_TYPE = "notification.push"


@dataclass
class NotifyResult:
    """Outcome of a single :func:`notify` dispatch.

    ``transport`` is the leg actually taken:

    - ``"ws"`` — the user had a live socket that accepted the frame; Web Push
      was skipped (the dedupe).
    - ``"push"`` — no live connection, so Web Push carried it.
    - ``"ws_fallback_push"`` — the user LOOKED live but the frame reached zero
      sockets (half-open / zombie), so Web Push carried it after all.

    ``ws_delivered`` is True only when the WS leg actually landed. ``send``
    carries the Web Push fan-out summary on both push legs (None on the WS
    leg, since Web Push never ran).
    """

    transport: str = "push"
    ws_delivered: bool = False
    send: SendResult | None = None


# ---------------------------------------------------------------------------
# Seams — injected so tests can drive the fork without real sockets. Both
# default to the live chat ConnectionManager singleton (the same instance the
# realtime bus fans WebSocket events through), imported lazily to avoid a
# module-import cycle (chat.ws → schemas → ... → push at collection time).
# ---------------------------------------------------------------------------


async def _is_user_live_elsewhere(user_id: str) -> bool:
    """True when another web process holds a live socket for the user. Always
    False unless ``POCKETPAW_REALTIME_BUS=redis-streams``."""
    from pocketpaw_ee.cloud._core.realtime import presence
    from pocketpaw_ee.cloud.chat.ws import manager

    return await presence.is_online_elsewhere(manager, user_id)


async def _relay_over_ws(user_id: str, payload: PushPayload) -> None:
    """Hand the notification frame to the other web processes, which deliver it
    to the user's sockets they hold."""
    from pocketpaw_ee.cloud._core.realtime import broadcast
    from pocketpaw_ee.cloud.chat.ws import manager

    channel = manager.relay_channel or broadcast.active_channel()
    if channel is None:
        return
    await channel.publish_group(
        f"notify:{user_id}",
        [user_id],
        WS_NOTIFICATION_TYPE,
        payload.model_dump(exclude_none=True),
    )


def _is_user_live(user_id: str) -> bool:
    """Return True when the user has at least one live WebSocket connection.

    Backed by ``chat.ws.ConnectionManager.is_online`` (ws.py:89) on the
    module singleton ``manager`` (ws.py:194) — the same registry the realtime
    bus uses to fan events to sockets, so "live" here means exactly "the bus
    could deliver to this user over WS right now".
    """
    from pocketpaw_ee.cloud.chat.ws import manager

    return manager.is_online(user_id)


async def _send_over_ws(user_id: str, payload: PushPayload) -> int:
    """Push a lightweight notification event to a user's live sockets.

    Returns the number of sockets that accepted the frame — 0 means the send
    reached nobody, which the caller treats as "not delivered" and falls back
    to Web Push.
    """
    from pocketpaw_ee.cloud.chat.schemas import WsOutbound
    from pocketpaw_ee.cloud.chat.ws import manager

    message = WsOutbound(
        type=WS_NOTIFICATION_TYPE,
        data=payload.model_dump(exclude_none=True),
    )
    return await manager.send_to_user(user_id, message)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def notify(
    workspace_id: str,
    user_id: str,
    payload: PushPayload | dict,
) -> NotifyResult:
    """Deliver one notification to a user, preferring WS over Web Push.

    The dedupe contract: if the user has a live WebSocket connection the
    notification is delivered over WS only — Web Push is intentionally NOT
    sent, so a user with both the desktop app and a browser tab open sees the
    notification exactly once. With no live connection the dispatch falls back
    to Web Push (``send_to_user``), so a browser-only or backgrounded user
    still gets it.

    Validates ``payload`` at entry (rule 6) so internal callers (bus
    listeners) may pass a raw dict. Returns a :class:`NotifyResult` recording
    the transport taken, for observability and tests.
    """
    payload = PushPayload.model_validate(payload)

    live_elsewhere = await _is_user_live_elsewhere(user_id)
    if live_elsewhere:
        await _relay_over_ws(user_id, payload)

    if _is_user_live(user_id):
        # Live desktop/browser client → WS only, skip Web Push (the dedupe).
        try:
            delivered = await _send_over_ws(user_id, payload)
        except Exception:
            # A WS failure must not silently drop the notification, but the
            # send_to_user fan-out is a separate path with its own pruning;
            # we log and report the WS leg rather than double-sending.
            logger.exception(
                "ws notification delivery failed for workspace=%s user=%s",
                workspace_id,
                user_id,
            )
            return NotifyResult(transport="ws", ws_delivered=False)

        if delivered or live_elsewhere:
            return NotifyResult(transport="ws", ws_delivered=True)

        # Looked live, reached nobody — every socket was half-open and got
        # pruned mid-send. The dedupe would drop the notification entirely, so
        # fall through to Web Push. Double-notify is not a risk here: zero
        # sockets received the WS event.
        logger.info(
            "ws notification reached no sockets, falling back to web push for workspace=%s user=%s",
            workspace_id,
            user_id,
        )
        result = await push_service.send_to_user(workspace_id, user_id, payload)
        return NotifyResult(transport="ws_fallback_push", ws_delivered=False, send=result)

    if live_elsewhere:
        # Delivered by the process holding the user's sockets.
        return NotifyResult(transport="ws", ws_delivered=True)

    # No live connection → Web Push fallback.
    result = await push_service.send_to_user(workspace_id, user_id, payload)
    return NotifyResult(transport="push", ws_delivered=False, send=result)


__all__ = ["NotifyResult", "WS_NOTIFICATION_TYPE", "notify"]
