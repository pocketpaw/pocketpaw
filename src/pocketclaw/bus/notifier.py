"""Channel notifier — push autonomous messages to configured targets.

Parses "channel:chat_id" strings from settings.notification_channels
and publishes OutboundMessage events to the message bus.
"""

import logging

from pocketclaw.bus.events import Channel, OutboundMessage

logger = logging.getLogger(__name__)

# Map lowercase channel name → Channel enum
_CHANNEL_MAP: dict[str, Channel] = {c.value: c for c in Channel}


async def notify(content: str, targets: list[str] | None = None) -> int:
    """Send a message to configured notification channels.

    Args:
        content: Text to send.
        targets: List of "channel:chat_id" strings.  If None, reads from
                 ``settings.notification_channels``.

    Returns:
        Number of messages successfully published.
    """
    if targets is None:
        from pocketclaw.config import get_settings

        targets = get_settings().notification_channels

    if not targets:
        return 0

    from pocketclaw.bus import get_message_bus

    bus = get_message_bus()
    count = 0

    for target in targets:
        if ":" not in target:
            logger.warning("Invalid notification target (missing ':'): %s", target)
            continue

        channel_str, chat_id = target.split(":", 1)
        channel = _CHANNEL_MAP.get(channel_str)
        if channel is None:
            logger.warning("Unknown channel in notification target: %s", channel_str)
            continue

        await bus.publish_outbound(
            OutboundMessage(channel=channel, chat_id=chat_id, content=content)
        )
        count += 1

    return count
