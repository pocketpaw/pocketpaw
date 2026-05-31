"""
Message bus for unified message routing.
Created: 2026-02-02
"""

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace

from pocketpaw.bus.events import Channel, InboundMessage, OutboundMessage, SystemEvent

logger = logging.getLogger(__name__)


class MessageBus:
    """
    Central message bus for all channel communication.

    Design Principles:
    - Single source of truth for message flow
    - Decouples channels from agent logic
    - Supports multiple subscribers per channel
    - Async-first with proper backpressure

    Usage:
        bus = MessageBus()

        # Subscribe to outbound messages for a channel
        bus.subscribe_outbound(Channel.TELEGRAM, telegram_sender)

        # Publish inbound (from channel adapter)
        await bus.publish_inbound(InboundMessage(...))

        # Consume inbound (in agent loop)
        msg = await bus.consume_inbound()
    """

    def __init__(self, max_queue_size: int = 1000):
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=max_queue_size)
        self._outbound_subscribers: dict[
            Channel, list[Callable[[OutboundMessage], Awaitable[None]]]
        ] = {}
        self._system_subscribers: list[Callable[[SystemEvent], Awaitable[None]]] = []

    # =========================================================================
    # Inbound (Channel → Agent)
    # =========================================================================

    async def publish_inbound(self, message: InboundMessage) -> None:
        """Publish a message from a channel adapter."""
        logger.debug(f"📥 Inbound: {message.channel.value}:{message.sender_id[:8]}...")
        await self._inbound.put(message)

    async def consume_inbound(self, timeout: float = 1.0) -> InboundMessage | None:
        """Consume the next inbound message (used by agent loop)."""
        try:
            return await asyncio.wait_for(self._inbound.get(), timeout=timeout)
        except TimeoutError:
            return None

    def inbound_pending(self) -> int:
        """Number of pending inbound messages."""
        return self._inbound.qsize()

    # =========================================================================
    # Outbound (Agent → Channel)
    # =========================================================================

    def subscribe_outbound(
        self, channel: Channel, callback: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        """Subscribe to outbound messages for a specific channel."""
        if channel not in self._outbound_subscribers:
            self._outbound_subscribers[channel] = []
        self._outbound_subscribers[channel].append(callback)
        logger.info(f"📡 Subscribed to {channel.value} outbound")

    def unsubscribe_outbound(
        self, channel: Channel, callback: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        """Unsubscribe from outbound messages."""
        if channel in self._outbound_subscribers:
            try:
                self._outbound_subscribers[channel].remove(callback)
            except ValueError:
                pass

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish message to all subscribers of the given channel."""
        subs = self._outbound_subscribers.get(msg.channel, [])
        if not subs:
            logger.warning(f"⚠️ No subscribers for {msg.channel.value}")
            return

        # Fan-out to all subscribers concurrently with deep isolation.
        # Each subscriber gets a deep copy of metadata and media to prevent leakage.
        async def _safe_publish(idx: int, callback: Callable[[OutboundMessage], Awaitable[None]]):
            try:
                # 1. Isolate mutable data for safety
                if msg.metadata or msg.media:
                    isolated_msg = replace(
                        msg,
                        metadata=copy.deepcopy(msg.metadata),
                        media=[copy.deepcopy(m) for m in msg.media],
                    )
                else:
                    isolated_msg = msg
            except Exception as e:
                logger.error(
                    f"⛔ Isolation FAILED for {msg.channel.value} subscriber {idx}; "
                    f"falling back to shallow copy (reduced isolation): {e}"
                )
                # Shallow copy fallback as a safer middle ground. Guard
                # against None explicitly — the dataclass defaults are empty
                # containers but a caller can still pass None at construction.
                isolated_msg = replace(
                    msg,
                    metadata=dict(msg.metadata) if msg.metadata else {},
                    media=list(msg.media) if msg.media else [],
                )

            # 2. Deliver message
            try:
                await callback(isolated_msg)
            except Exception as e:
                logger.error(
                    f"❌ Delivery FAILED for {msg.channel.value} subscriber {idx} ({callback}): {e}"
                )
                raise  # Re-raise to let gather capture it

        await asyncio.gather(
            *[_safe_publish(i, sub) for i, sub in enumerate(subs)], return_exceptions=True
        )

    async def broadcast_outbound(
        self, msg: OutboundMessage, exclude: Channel | None = None
    ) -> None:
        """Broadcast an outbound message to ALL registered channels."""
        # This is used for multi-channel announcements.
        for channel in list(self._outbound_subscribers.keys()):
            if channel == exclude:
                continue
            # Create a clone for each channel
            channel_msg = replace(msg, channel=channel)
            try:
                await self.publish_outbound(channel_msg)
            except Exception as e:
                logger.error(f"🚨 Broadcast to channel {channel.value} FAILED: {e}")

    # =========================================================================
    # System Events (Internal)
    # =========================================================================

    def subscribe_system(self, callback: Callable[[SystemEvent], Awaitable[None]]) -> None:
        """Subscribe to system events."""
        self._system_subscribers.append(callback)

    def unsubscribe_system(self, callback: Callable[[SystemEvent], Awaitable[None]]) -> None:
        """Unsubscribe from system events."""
        try:
            self._system_subscribers.remove(callback)
        except ValueError:
            pass

    async def publish_system(self, event: SystemEvent) -> None:
        """Publish a system event."""
        tasks = [sub(event) for sub in self._system_subscribers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "System subscriber %d failed for '%s': %s",
                    i,
                    event.event_type,
                    result,
                )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def clear(self) -> None:
        """Clear all queues (for testing/reset)."""
        while not self._inbound.empty():
            try:
                self._inbound.get_nowait()
            except asyncio.QueueEmpty:
                break


# Singleton instance
_bus: MessageBus | None = None


def get_message_bus() -> MessageBus:
    """Get the global message bus instance."""
    global _bus
    if _bus is None:
        _bus = MessageBus()

        from pocketpaw.lifecycle import register

        def _reset():
            global _bus
            _bus = None

        register("message_bus", reset=_reset)
    return _bus
