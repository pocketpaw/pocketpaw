"""External channel conversations ↔ workspace chat rooms (T-9).

Created 2026-08-08 (feat/coupling-t9-channels-in-chat).

PocketPaw can talk on Telegram/WhatsApp/Slack/Discord, and the workspace has
chat rooms, and until now those were two disconnected worlds. A customer
messaging the Telegram bot was invisible in the app — the team would have to
open Telegram to see it, and could not reply from the room they actually work
in. Two message stores (the OSS bus + Mongo ``messages``) that never met.

A group with a ``channel_binding`` now mirrors one external conversation:

    customer ──▶ adapter ──▶ OSS bus ──▶ [observer] ──▶ room message
    room post ──▶ message.sent ──▶ [subscriber] ──▶ OSS outbound ──▶ customer
    agent reply ──▶ OSS outbound ──▶ [subscriber] ──▶ room message

THE THREE ECHO LOOPS (read before changing any handler)
--------------------------------------------------------
A bidirectional mirror is a ring. Each direction must be prevented from
feeding the other, and two of the three guards here are STRUCTURAL rather
than flag-based, which is why they are worth writing down:

1. **Room post → channel → back into the room.** A human posting in the room
   emits ``message.sent``; we publish an ``OutboundMessage``; our OWN outbound
   subscriber would then mirror that straight back into the room as a
   duplicate. Guarded by ``_BRIDGE_MARKER`` in the outbound metadata —
   ``_on_outbound`` skips anything carrying it. This is the one FLAG guard.

2. **Inbound mirror → back out to the customer.** Mirroring a customer's
   message into the room must not send that message back to them. STRUCTURAL:
   ``_mirror_into_room`` writes via ``_create_group_message_doc`` and never
   emits ``message.sent``, so ``_on_room_message`` — which only ever fires on
   ``message.sent`` — cannot see it. There is no flag to forget.

3. **Inbound mirror → group agent auto-reply → double reply.** The customer's
   message is ALREADY on its way to the agent via the OSS queue (that is what
   the adapter published it for). If mirroring it also triggered the room's
   group agents, the customer would get two answers. STRUCTURAL, same
   mechanism: ``message.sent`` is what ``shared/agent_bridge`` subscribes to,
   and the mirror does not emit it. This is exactly why the agent-stream
   persist helpers in ``message_service`` bypass ``send_message`` too.

The mirror DOES emit ``MessageNew`` so the room updates live — that is the
realtime bus, which drives the FE and triggers nothing.

SCOPE
-----
Content only. ``media`` is not mirrored in either direction; an image sent on
Telegram shows up in the room as its caption text (or empty). Agent replies
mirror into the room but a group agent replying IN the room does not go out to
the customer — the customer's agent answer already travels the OSS path, and
adding a second producer would need a fourth loop guard for no user-visible
gain. Both are follow-ups, not oversights.
"""

from __future__ import annotations

import logging
from typing import Any

from pocketpaw.bus.events import Channel, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)

# Stamped into the metadata of every OutboundMessage this bridge publishes, so
# our own outbound subscriber can tell "a room post on its way to the customer"
# from "an agent reply worth showing the team". Loop guard #1.
_BRIDGE_MARKER = "_paw_chat_bridge"

# sender_type for a mirrored external participant. They are neither a workspace
# user (no account) nor an agent, and calling them either would put a stranger's
# name where a colleague's belongs.
_EXTERNAL_SENDER_TYPE = "external"


async def _resolve_binding(channel: str, chat_id: str) -> tuple[str, str] | None:
    from pocketpaw_ee.cloud.chat import group_service

    return await group_service.find_group_id_by_channel_binding(channel, chat_id)


async def _mirror_into_room(
    *,
    group_id: str,
    workspace_id: str,
    content: str,
    sender_type: str,
    sender_name: str | None,
    agent_id: str | None = None,
) -> str | None:
    """Write one mirrored message into a room and light it up live.

    Deliberately NOT ``send_message``: that emits ``message.sent``, which is
    both the agent-bridge auto-respond trigger and this bridge's own
    room→channel trigger. See loop guards 2 and 3 in the module docstring.
    """
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud.chat import group_service, message_service
    from pocketpaw_ee.cloud.chat.dto import message_to_wire_dict
    from pocketpaw_ee.cloud.realtime.emit import emit
    from pocketpaw_ee.cloud.realtime.events import MessageNew

    domain_msg = await message_service._create_group_message_doc(
        group_id=group_id,
        sender=None,
        sender_type=sender_type,
        sender_name=sender_name,
        agent=agent_id,
        content=content,
    )
    await group_service.bump_message_stats(
        group_id, last_message_at=domain_msg.created_at or datetime.now(UTC)
    )
    wire = message_to_wire_dict(domain_msg)
    await emit(MessageNew(data={**wire, "group_id": group_id, "workspace_id": workspace_id}))
    return domain_msg.id


# ---------------------------------------------------------------------------
# channel → room
# ---------------------------------------------------------------------------


async def _on_inbound(message: InboundMessage) -> None:
    """OSS inbound OBSERVER — never consumes, never raises.

    Registered via ``subscribe_inbound_observer`` rather than a consumer: the
    agent loop is the consumer of record and stealing its messages would stop
    the agent replying at all.
    """
    try:
        binding = await _resolve_binding(str(message.channel.value), message.chat_id)
        if binding is None:
            return  # unbound conversation — the overwhelming default
        group_id, workspace_id = binding
        content = (message.content or "").strip()
        if not content:
            return  # media-only message; media mirroring is a follow-up
        await _mirror_into_room(
            group_id=group_id,
            workspace_id=workspace_id,
            content=content,
            sender_type=_EXTERNAL_SENDER_TYPE,
            sender_name=_display_name(message),
        )
    except Exception:
        logger.exception("channel → room mirror failed (the agent still got the message)")


def _display_name(message: InboundMessage) -> str:
    """Best available human label for an external sender.

    Adapters vary in what they know, so this reads the metadata names they do
    set and falls back to the channel + a truncated sender id. Never empty:
    an unlabelled row in a room is worse than a crude one.
    """
    meta: dict[str, Any] = message.metadata or {}
    for key in ("sender_name", "full_name", "username", "from_name"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return f"{message.channel.value}:{(message.sender_id or 'unknown')[:12]}"


# ---------------------------------------------------------------------------
# agent reply → room
# ---------------------------------------------------------------------------


async def _on_outbound(message: OutboundMessage) -> None:
    """Mirror the agent's reply into the room so the team sees both sides.

    Loop guard #1 lives here: a message this bridge itself published (a room
    post on its way to the customer) carries ``_BRIDGE_MARKER`` and is
    skipped, or it would be echoed straight back into the room it came from.
    """
    try:
        if (message.metadata or {}).get(_BRIDGE_MARKER):
            return
        if message.is_stream_chunk and not message.is_stream_end:
            return  # mirror the finished reply, not every token
        binding = await _resolve_binding(str(message.channel.value), message.chat_id)
        if binding is None:
            return
        group_id, workspace_id = binding
        content = (message.content or "").strip()
        if not content:
            return
        await _mirror_into_room(
            group_id=group_id,
            workspace_id=workspace_id,
            content=content,
            sender_type="agent",
            sender_name="Agent",
        )
    except Exception:
        logger.exception("agent reply → room mirror failed (the customer still got the reply)")


# ---------------------------------------------------------------------------
# room → channel
# ---------------------------------------------------------------------------


async def _on_room_message(payload: dict[str, Any]) -> None:
    """``message.sent`` subscriber — a human posted in a room; send it out.

    ``message.sent`` fires ONLY on the human ``send_message`` path, which is
    what makes loop guards 2 and 3 structural: neither the inbound mirror nor
    the agent mirror emits it, so neither can reach this handler.
    """
    try:
        data = payload if isinstance(payload, dict) else {}
        group_id = data.get("group_id")
        content = (data.get("content") or "").strip()
        if not group_id or not content:
            return

        from pocketpaw_ee.cloud.chat import group_service

        group = await group_service._get_group_or_404(str(group_id))
        binding = getattr(group, "channel_binding", None)
        if binding is None:
            return  # an ordinary room — the overwhelming default

        try:
            channel = Channel(binding.channel)
        except ValueError:
            logger.warning(
                "room %s is bound to unknown channel %r — not sending", group_id, binding.channel
            )
            return

        from pocketpaw.bus import get_message_bus

        await get_message_bus().publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=binding.chat_id,
                content=content,
                # Loop guard #1 — see _on_outbound.
                metadata={_BRIDGE_MARKER: True, "group_id": str(group_id)},
            )
        )
    except Exception:
        logger.exception("room → channel send failed (the room message is still posted)")


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register_channel_chat_listeners() -> None:
    """Idempotently wire the mirror in both directions.

    Unsubscribe-first on every seam, matching the alerts bridge: the OSS bus
    singleton is reset between tests, and a module-level "already registered"
    flag would silently skip re-registration against the fresh bus (FU-14 is
    that bug in another module).
    """
    from pocketpaw.bus import get_message_bus
    from pocketpaw_ee.cloud.shared.events import event_bus

    bus = get_message_bus()
    bus.unsubscribe_inbound_observer(_on_inbound)
    bus.subscribe_inbound_observer(_on_inbound)

    # Outbound is per-channel, so subscribe across every channel a binding
    # could name rather than hand-maintaining a second list that drifts.
    for channel in Channel:
        bus.unsubscribe_outbound(channel, _on_outbound)
        bus.subscribe_outbound(channel, _on_outbound)

    event_bus.unsubscribe("message.sent", _on_room_message)
    event_bus.subscribe("message.sent", _on_room_message)
    logger.info("registered channel ↔ chat mirror")


__all__ = ["register_channel_chat_listeners"]
