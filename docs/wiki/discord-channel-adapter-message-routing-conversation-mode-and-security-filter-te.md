---
{
  "title": "Discord Channel Adapter: Message Routing, Conversation Mode, and Security Filter Tests",
  "summary": "This suite tests PocketPaw's DiscliAdapter, which connects the agent runtime to Discord via the discli CLI tool. It covers channel property defaults, status type validation, start/stop lifecycle, inbound message handling with guild/user allow-lists, conversation mode with history context, outbound message delivery, and the no-response marker that suppresses bot replies in monitored channels.",
  "concepts": [
    "DiscliAdapter",
    "Discord",
    "discli",
    "Channel_DISCORD",
    "InboundMessage",
    "OutboundMessage",
    "guild_allowlist",
    "user_allowlist",
    "conversation_mode",
    "conversation_history",
    "bot_author_key",
    "no_response_marker",
    "status_type",
    "message_bus",
    "channel_adapter"
  ],
  "categories": [
    "testing",
    "channel-adapters",
    "discord",
    "message-routing",
    "security",
    "test"
  ],
  "source_docs": [
    "bea0ec9140eb8879"
  ],
  "backlinks": null,
  "word_count": 469,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_discord_adapter.py` tests `pocketpaw.bus.adapters.discord_adapter.DiscliAdapter`, PocketPaw's Discord integration built on top of the `discli` CLI tool. The adapter translates between Discord events and PocketPaw's internal `InboundMessage`/`OutboundMessage` bus format, implementing allow-list filtering, conversation history management, and bot self-message suppression.

## Why This Module Exists

Discord is a primary deployment channel for PocketPaw agents. The adapter must handle the full complexity of Discord's event model — multiple guilds, DMs, bot messages, message threading — while keeping the core agent runtime decoupled from Discord-specific logic.

## Basic Properties and Defaults

- `test_channel_property`: The adapter reports `Channel.DISCORD` to the bus router.
- `test_status_defaults_to_online`: An invalid `status_type` silently defaults to `"online"` rather than raising. This prevents misconfigured deployments from failing to connect.
- `test_valid_status_types`: The four valid Discord presence types (`online`, `idle`, `dnd`, `invisible`) are all accepted.

## Start/Stop Lifecycle

`test_start_stop` exercises the adapter's `start()` and `stop()` methods, confirming the adapter connects to Discord on start and cleans up on stop. The test patches the underlying `discli` process to avoid real network connections.

## Inbound Message Handling — Allow-Lists

The adapter enforces two security filters:
- **Guild allow-list**: Only messages from approved guild IDs are processed.
- **User allow-list**: Only messages from approved user IDs are processed.

These allow-lists prevent the agent from responding to arbitrary Discord servers or users that happen to be in the same channel. Without them, a public bot would respond to any user who could see the channel.

## Conversation Mode

`convo_adapter` is configured with specific `conversation_channel_ids`. In conversation mode, the adapter maintains a sliding window of recent messages as context (`_CONVERSATION_HISTORY_SIZE`) and prepends them to the user's message before publishing to the bus. This gives the agent memory of the recent conversation thread without requiring it to access the full Discord message history API.

The `_CONVERSATION_CHAR_BUDGET` constant limits total context size to prevent overflowing the agent's context window with conversation history from high-traffic channels.

## Bot Self-Message Suppression

The `_BOT_AUTHOR_KEY` identifies messages sent by the bot itself. The adapter uses this to skip processing its own messages, preventing infinite response loops where the bot responds to its own output.

## No-Response Marker

`_NO_RESPONSE_MARKER` is a special string that agents can include in their response to signal that no Discord message should be sent. This allows agents to "listen" to channels without responding — useful for monitoring channels where the agent collects data but should not acknowledge receipt.

## Outbound Message Delivery

Tests verify that `OutboundMessage` objects published to the bus are correctly translated to Discord API calls, including: channel ID routing, message content formatting, and attachment handling.

## Known Gaps

Rate limiting behavior (Discord's 5 messages/5 seconds per channel limit) is not tested. Long message splitting (Discord's 2000-character message limit requires splitting long responses) may not be fully covered. Reaction-based interaction patterns (thumbs-up/down for feedback) are not tested.
