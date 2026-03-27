import asyncio
import logging
from pocketpaw.logging_setup import setup_logging
from pocketpaw.context import set_correlation_id, get_correlation_id
from pocketpaw.bus.events import InboundMessage, Channel
from pocketpaw.bus.adapters import BaseChannelAdapter
from unittest.mock import MagicMock

async def test_tracing():
    setup_logging(level="INFO")
    logger = logging.getLogger("test_tracing")
    
    # 1. Test basic context propagation in logging
    set_correlation_id("test-trace-123")
    logger.info("This log should have the correlation ID")
    
    # 2. Test adapter injection
    class MockBus:
        def __init__(self):
            self.published = []
        def subscribe_outbound(self, channel, callback): ...
        async def publish_inbound(self, msg):
            self.published.append(msg)
            
    class ConcreteAdapter(BaseChannelAdapter):
        @property
        def channel(self): return Channel.TELEGRAM
        async def send(self, message): pass
        async def _on_start(self): pass
        async def _on_stop(self): pass

    bus = MockBus()
    adapter = ConcreteAdapter()
    adapter._bus = bus
    
    msg = InboundMessage(
        channel=Channel.TELEGRAM,
        sender_id="user1",
        chat_id="chat1",
        content="hello"
    )
    
    # This should inject a new UUID and set it in context
    import pocketpaw.context
    pocketpaw.context._correlation_id.set(None) # Clear for test
    
    await adapter._publish_inbound(msg)
    
    injected_msg = bus.published[0]
    print(f"Injected ID: {injected_msg.correlation_id}")
    assert injected_msg.correlation_id != "unset"
    assert get_correlation_id() == injected_msg.correlation_id
    
    logger.info("This log should have the NEW AUTO-GENERATED correlation ID")

if __name__ == "__main__":
    asyncio.run(test_tracing())
