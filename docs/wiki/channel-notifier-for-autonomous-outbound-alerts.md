---
{
  "title": "Channel Notifier for Autonomous Outbound Alerts",
  "summary": "notifier.py provides a `notify()` function that lets any part of PocketPaw push a message to configured notification channels without knowing which adapters are active. It parses `channel:chat_id` target strings, resolves them to `Channel` enum values, and publishes `OutboundMessage` events to the bus.",
  "concepts": [
    "notify",
    "notification_channels",
    "channel:chat_id format",
    "OutboundMessage",
    "bus.publish_outbound",
    "proactive messaging",
    "settings fallback",
    "_CHANNEL_MAP",
    "autonomous alerts"
  ],
  "categories": [
    "bus",
    "notifications",
    "messaging",
    "utilities"
  ],
  "source_docs": [
    "07b1f5b282cdbbcd"
  ],
  "backlinks": null,
  "word_count": 411,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`notify()` is the single entry point for autonomous, non-conversational outbound messages. While channel adapters send replies in response to `InboundMessage` events, `notify()` is for proactive messages: deployment alerts, scheduled digests, error notifications, or any server-initiated push to a user.

## Target String Format

Targets are specified as `"channel:chat_id"` strings (e.g., `"telegram:123456789"`, `"slack:C01ABC123"`, `"whatsapp:+15551234567"`). This compact format is easy to store in settings files (`settings.notification_channels`) or pass as function arguments without requiring callers to import `Channel` enum values.

The `_CHANNEL_MAP` dictionary is built once at module load time by iterating `Channel` enum members: `{c.value: c for c in Channel}`. This means any new channel added to the `Channel` enum is automatically supported in `notify()` without changes to notifier.py.

## Fallback to Settings

If `targets` is `None`, `notify()` reads `settings.notification_channels` — a list of target strings configured by the operator. This allows the function to be called as `await notify("Server restarted")` with no explicit target, relying on the operator's pre-configured notification channels. Passing an explicit `targets` list overrides this for one-off alerts to specific recipients.

## Error Handling

Malformed targets (missing `:` separator) are logged as warnings and skipped. Unknown channel names are also logged and skipped. The function returns the count of successfully published messages, allowing callers to detect silent failures.

Targets with valid format but no active adapter subscriber will result in the message bus logging `"No subscribers for {channel}"` — the notifier itself does not verify that an adapter is running for the target channel.

## Bus Integration

Each valid target produces an `OutboundMessage` published via `bus.publish_outbound()`. The bus fans this message out to all subscribers registered for that channel, which includes the actual adapter that handles delivery. The notifier has no direct reference to any adapter — it only knows the bus.

## Use Cases

- **Server lifecycle events**: notify when PocketPaw starts, stops, or encounters a critical error
- **Scheduled agent outputs**: send a daily digest to a Telegram channel
- **Tool callbacks**: alert the operator when a long-running tool completes
- **Monitoring integration**: bridge external alert systems (PagerDuty, Datadog) into a messaging channel

## Known Gaps

- There is no retry logic for failed deliveries. If the target adapter's `send()` raises an exception, the notifier does not detect it (exceptions are caught within the bus's `asyncio.gather()` call and logged there).
- There is no deduplication or rate limiting — calling `notify()` in a tight loop will produce a flood of messages to the target channel.