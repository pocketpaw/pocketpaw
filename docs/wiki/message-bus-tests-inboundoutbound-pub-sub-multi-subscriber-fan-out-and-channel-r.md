---
{
  "title": "Message Bus Tests: Inbound/Outbound Pub-Sub, Multi-Subscriber Fan-out, and Channel Routing",
  "summary": "This test suite validates PocketPaw's internal message bus, which decouples channel adapters (Telegram, Discord, WhatsApp, CLI) from the core agent logic using an async publish-subscribe queue. Tests cover inbound message queueing, outbound pub-sub subscription and delivery, multi-subscriber fan-out, unsubscribe, adapter integration, broadcast to all channels, and per-channel filtering.",
  "concepts": [
    "MessageBus",
    "InboundMessage",
    "OutboundMessage",
    "Channel",
    "publish-subscribe",
    "BaseChannelAdapter",
    "fan-out",
    "unsubscribe",
    "broadcast",
    "channel routing"
  ],
  "categories": [
    "message bus",
    "testing",
    "pub-sub",
    "channel adapters",
    "test"
  ],
  "source_docs": [
    "dd910aeec8900eef"
  ],
  "backlinks": null,
  "word_count": 368,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw uses a central `MessageBus` to route messages between channel adapters and the agent processing core. Channel adapters publish inbound messages to the bus and subscribe to outbound messages the agent produces. This decoupling means the agent core never imports Telegram or Discord SDK code directly — it only works with `InboundMessage` and `OutboundMessage` events.

## Inbound Flow

```python
async def test_inbound_flow():
    bus = MessageBus()
    msg = InboundMessage(
        channel=Channel.CLI, sender_id="user1",
        chat_id="chat1", content="Hello",
    )
    await bus.publish_inbound(msg)
    assert bus.inbound_pending() == 1
```

The inbound queue buffers messages from all channels until the agent processor dequeues them. `inbound_pending()` lets monitoring code check queue depth — high pending counts indicate the agent is processing slowly or has stalled.

## Outbound Pub-Sub

```python
async def test_outbound_pubsub():
    await bus.subscribe_outbound(Channel.CLI, handler)
    await bus.publish_outbound(OutboundMessage(channel=Channel.CLI, content="Response"))
    assert len(received) == 1
```

The outbound side uses channel-keyed subscriptions: adapters subscribe to their own channel and receive only messages destined for that channel. This prevents Telegram messages from being accidentally delivered to the Discord adapter.

## Multi-Subscriber Fan-out

```python
async def test_outbound_multiple_subscribers():
    # Two handlers subscribed to CLI
    # One publish delivers to both
    assert len(received1) == 1
    assert len(received2) == 1
```

Multiple subscribers to the same channel are all notified. This supports scenarios like a logging subscriber that records all outbound messages alongside the actual delivery subscriber.

## Unsubscribe

```python
async def test_unsubscribe():
    token = await bus.subscribe_outbound(Channel.CLI, handler)
    await bus.unsubscribe_outbound(token)
    await bus.publish_outbound(...)
    assert len(received) == 0
```

Unsubscribe uses a token returned at subscribe time, which avoids the problem of passing the same function reference for removal (unreliable with closures and bound methods).

## Adapter Integration

```python
async def test_adapter_integration():
    class MockAdapter(BaseChannelAdapter):
        @property
        def channel(self): return Channel.CLI
        async def send(self, message): ...
```

This test verifies that a concrete `BaseChannelAdapter` subclass works end-to-end with the bus. `BaseChannelAdapter.send()` is the contract all adapters must implement.

## Per-Channel Coverage

The suite includes separate pub-sub tests for Discord, Slack, and WhatsApp channels, guarding against typos in `Channel` enum values or mis-mapped channel routing logic.

## Known Gaps

No test covers what happens when an outbound subscriber's handler raises an exception — whether the bus swallows the error and continues to other subscribers, or propagates it, is not validated.