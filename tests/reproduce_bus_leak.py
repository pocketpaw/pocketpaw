import asyncio
import copy
from dataclasses import replace
import pytest
from pocketpaw.bus.events import Channel, OutboundMessage
from pocketpaw.bus.queue import MessageBus

@pytest.mark.asyncio
async def test_outbound_message_isolation():
    bus = MessageBus()
    
    # Track what each subscriber received
    received_messages = []
    
    async def sub1(msg):
        # Modify the message in the first subscriber
        msg.metadata["leaked"] = "yes"
        received_messages.append(copy.deepcopy(msg))
        
    async def sub2(msg):
        # Wait a bit to ensure sub1 runs first
        await asyncio.sleep(0.1)
        received_messages.append(copy.deepcopy(msg))
        
    bus.subscribe_outbound(Channel.TELEGRAM, sub1)
    bus.subscribe_outbound(Channel.TELEGRAM, sub2)
    
    test_msg = OutboundMessage(
        channel=Channel.TELEGRAM,
        chat_id="test_chat",
        content="Hello",
        metadata={"original": "value"}
    )
    
    await bus.publish_outbound(test_msg)
    
    assert len(received_messages) == 2
    # sub1 should have the modification
    assert received_messages[0].metadata.get("leaked") == "yes"
    # sub2 should NOT have the modification
    assert "leaked" not in received_messages[1].metadata
    assert received_messages[1].metadata["original"] == "value"
    print("\n✅ Isolation verified: Subscriber 2 did not see Subscriber 1's modification.")

if __name__ == "__main__":
    asyncio.run(test_outbound_message_isolation())
