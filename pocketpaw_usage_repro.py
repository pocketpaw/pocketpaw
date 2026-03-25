import asyncio
from unittest.mock import patch
from pocketpaw.api.v1.chat import _APISessionBridge
from pocketpaw.bus.queue import MessageBus
from pocketpaw.bus.events import Channel, OutboundMessage, SystemEvent

async def main():
    bus = MessageBus()
    chat_id = "abc123"
    bridge = _APISessionBridge(chat_id)

    with patch("pocketpaw.bus.get_message_bus", return_value=bus):
        await bridge.start()

        # Simulate token usage event
        await bus.publish_system(
            SystemEvent(
                event_type="token_usage",
                data={
                    "session_key": f"websocket:{chat_id}",
                    "input_tokens": 10,
                    "output_tokens": 5
                },
            )
        )

        # Simulate stream_end without usage
        await bus.publish_outbound(
            OutboundMessage(
                channel=Channel.WEBSOCKET,
                chat_id=chat_id,
                content="",
                is_stream_end=True,
                metadata={},
            )
        )

        first = await bridge.queue.get()
        print("event:", first)

        await bridge.stop()

asyncio.run(main())