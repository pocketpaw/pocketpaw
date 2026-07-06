# Notification dispatch + WS-vs-Web-Push dedupe (pocketpaw#1393).
# Created: 2026-06-09 (feat/push-wire-events) — adds ``notify`` on top of the
# #1392 ``send_to_user`` Web Push fan-out. ``notify`` is the single dispatch
# product events call: it forks the transport so a user who has BOTH the
# desktop app (live WebSocket) and a browser tab (Web Push) open is never
# double-notified.
#
# The dedupe rule (issue #1393 "prefer WS when live, else push"):
#   - LIVE WebSocket connection  → deliver a ``notification.push`` WS event
#     the desktop/Tauri client already renders; do NOT also send Web Push.
#   - No live connection         → fall back to ``send_to_user`` (Web Push),
#     so a browser-only / backgrounded user still gets the notification.
#
# This module is a thin orchestrator — it imports the push *service* for the
# Web Push leg and the chat WS ConnectionManager for the liveness check + WS
# leg. It performs NO Beanie writes itself (the import-linter "Push — Beanie
# writes only from service.py" contract stays satisfied: the only writer is
# ``service.send_to_user`` which prunes dead rows as before). The liveness
# check and the chosen transport are surfaced on a returned ``NotifyResult``
# so the fork is observable and unit-testable without real sockets.

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

    ``transport`` is the leg actually taken — ``"ws"`` when the user had a
    live WebSocket connection (Web Push was skipped) or ``"push"`` when it
    fell back to Web Push. ``ws_delivered`` is True only on the WS leg.
    ``send`` carries the Web Push fan-out summary on the push leg (None on
    the WS leg, since Web Push never ran).
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


def _is_user_live(user_id: str) -> bool:
    """Return True when the user has at least one live WebSocket connection.

    Backed by ``chat.ws.ConnectionManager.is_online`` (ws.py:89) on the
    module singleton ``manager`` (ws.py:194) — the same registry the realtime
    bus uses to fan events to sockets, so "live" here means exactly "the bus
    could deliver to this user over WS right now".
    """
    from pocketpaw_ee.cloud.chat.ws import manager

    return manager.is_online(user_id)


async def _send_over_ws(user_id: str, payload: PushPayload) -> None:
    """Push a lightweight notification event to a user's live sockets."""
    from pocketpaw_ee.cloud.chat.schemas import WsOutbound
    from pocketpaw_ee.cloud.chat.ws import manager

    message = WsOutbound(
        type=WS_NOTIFICATION_TYPE,
        data=payload.model_dump(exclude_none=True),
    )
    await manager.send_to_user(user_id, message)


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

    if _is_user_live(user_id):
        # Live desktop/browser client → WS only, skip Web Push (the dedupe).
        try:
            await _send_over_ws(user_id, payload)
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
        return NotifyResult(transport="ws", ws_delivered=True)

    # No live connection → Web Push fallback.
    result = await push_service.send_to_user(workspace_id, user_id, payload)
    return NotifyResult(transport="push", ws_delivered=False, send=result)


__all__ = ["NotifyResult", "WS_NOTIFICATION_TYPE", "notify"]
