---
{
  "title": "Channel Notifier Tests: Multi-Target Message Dispatch, Validation, and Settings-Driven Defaults",
  "summary": "This test suite validates `notify()`, PocketPaw's high-level function for pushing outbound messages to one or more channels. Tests cover multi-target fan-out, graceful handling of malformed target strings, unknown channel names, empty target lists, settings-driven default targets, chat IDs containing colons, and delivery across all supported channels.",
  "concepts": [
    "notify",
    "OutboundMessage",
    "Channel",
    "MessageBus",
    "target string",
    "chat_id",
    "Settings",
    "multi-target",
    "malformed targets",
    "channel routing"
  ],
  "categories": [
    "channel notifier",
    "testing",
    "outbound messaging",
    "notification",
    "test"
  ],
  "source_docs": [
    "4d3f472ebec7d29a"
  ],
  "backlinks": null,
  "word_count": 384,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`notify()` is PocketPaw's primary outbound messaging primitive, used by reminders, agent responses, and system events to deliver text to users across channels. A target string has the format `"channel:chat_id"` (e.g., `"telegram:123456"`, `"discord:987654321"`). This test file validates the full dispatch logic including edge cases that could silently drop messages.

## Multi-Target Fan-out

```python
async def test_publishes_for_each_target(self, mock_bus_fn):
    count = await notify("Hello!", targets=["telegram:123", "discord:456"])
    assert count == 2
    assert bus.publish_outbound.call_count == 2
    msg1 = bus.publish_outbound.call_args_list[0][0][0]
    assert msg1.channel == Channel.TELEGRAM
```

The test verifies both the count return value and that each `OutboundMessage` carries the correct `Channel` enum value. If channel mapping were broken, a Telegram message might be routed to the Discord adapter.

## Malformed Target Handling

```python
async def test_skips_invalid_target_no_colon(self, mock_bus_fn):
    count = await notify("Hello!", targets=["not-a-valid-target"])
    assert count == 0
    bus.publish_outbound.assert_not_called()

async def test_skips_unknown_channel(self, mock_bus_fn):
    count = await notify("Hello!", targets=["fax:12345"])
    assert count == 0
```

Malformed targets and unknown channel names are silently skipped with a count of 0 rather than raising. A misconfigured reminder target should not crash the entire scheduler.

## Chat IDs Containing Colons

```python
async def test_chat_id_with_colons(self, mock_bus_fn):
    count = await notify("Hello!", targets=["telegram:ch:123456"])
    assert count == 1
```

Some platforms use chat IDs that themselves contain colons (e.g., Matrix room IDs). The split must only split on the first colon, treating everything after as the chat ID. This test guards against a naive `target.split(":")` that would break on such IDs.

## Settings-Driven Default Targets

```python
async def test_reads_from_settings_when_targets_none(self, mock_settings, mock_bus_fn):
    count = await notify("Alert!", targets=None)
    # Delivers to all channels configured in settings
```

When `targets` is not specified, `notify()` falls back to the operator-configured default notification channels in `Settings`. This is the mode used by the reminder system and agent-initiated messages.

## All Known Channels

```python
async def test_all_known_channels(self, mock_bus_fn):
    targets = [f"{ch.value}:123" for ch in Channel]
    count = await notify("test", targets=targets)
    assert count == len(list(Channel))
```

This test acts as a registry guard: if a new `Channel` enum member is added but `notify()` doesn't know how to route it, the count will be less than expected and the test fails.

## Known Gaps

No test covers what happens when `bus.publish_outbound()` raises an exception for one target — whether the error is caught and the remaining targets still receive the message, or whether the entire `notify()` call fails.