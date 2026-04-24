---
{
  "title": "InProcessBus Tests: Fan-Out Correctness, Error Isolation, and Module Singleton",
  "summary": "These tests verify InProcessBus — the in-process real-time event bus — covering correct fan-out to all resolved recipients, the WsOutbound payload shape, per-recipient exception isolation so one dead socket does not stop others, graceful audience resolution error swallowing, and the module-level singleton get/set contract.",
  "concepts": [
    "InProcessBus",
    "fan-out",
    "WebSocket delivery",
    "WsOutbound",
    "exception isolation",
    "audience resolution",
    "module singleton",
    "get_bus",
    "set_bus",
    "ConnectionManager",
    "error swallowing"
  ],
  "categories": [
    "testing",
    "real-time",
    "event bus",
    "fault tolerance",
    "test"
  ],
  "source_docs": [
    "901666e0b782801a"
  ],
  "backlinks": null,
  "word_count": 304,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`InProcessBus` connects the event system to the WebSocket layer. When `publish(ev)` is called, it resolves the audience for the event, then calls `conn_manager.send_to_user()` for each recipient. The tests verify this pipeline under normal conditions and failure modes.

## Fan-Out Correctness

```python
async def test_inprocess_bus_fans_out_to_resolved_audience():
    ev = GroupCreated(data={"group_id": "g1", "member_ids": ["u1", "u2"]})
    await bus.publish(ev)
    assert conn.send_to_user.await_count == 2
    sent = {call.args[0] for call in conn.send_to_user.await_args_list}
    assert sent == {"u1", "u2"}
```

Set comparison on `args[0]` (user ID) checks both users were targeted without caring about delivery order.

## Payload Shape

```python
async def test_inprocess_bus_sends_correct_payload():
    user_arg, payload = conn.send_to_user.await_args.args
    assert payload.type == "message.sent"
    assert payload.data == {"group_id": "g", "sender_id": "u1"}
```

The payload sent over WebSocket is a `WsOutbound` object with `type` (the event's wire type) and `data` (the event's data dict). Both fields are verified to catch future serialization format changes.

## Per-Recipient Exception Isolation

```python
async def test_inprocess_bus_isolates_per_recipient_exceptions():
    conn.send_to_user.side_effect = [None, RuntimeError("dead socket"), None]
    await bus.publish(ev)  # 3 recipients
    assert conn.send_to_user.await_count == 3
```

If one recipient's WebSocket is closed, the bus must continue delivering to remaining recipients. Without isolation, a single dead socket would block all subsequent deliveries. The bus wraps each `send_to_user` call in a `try/except`.

## Audience Resolution Error Swallowing

```python
async def test_inprocess_bus_swallows_audience_resolution_errors():
    class BrokenResolver:
        async def audience(self, _ev):
            raise RuntimeError("db exploded")
    await bus.publish(GroupCreated(...))
    conn.send_to_user.assert_not_called()
```

Mutation operations call `emit` as a side effect. If emit propagated resolver failures, a database outage would cause every chat message to fail even though the message was successfully saved. Swallowing resolver errors keeps emit best-effort.

## Module Singleton Pattern

```python
def test_module_singleton_get_raises_if_not_set():
    bus_mod._bus = None
    with pytest.raises(AssertionError):
        get_bus()

def test_module_singleton_set_then_get():
    set_bus(dummy)
    assert get_bus() is dummy
```

`get_bus()` raises `AssertionError` if accessed before initialization — a programming error, not a runtime condition, so an assertion is appropriate.

## Known Gaps

None identified.