---
{
  "title": "Discord Channel Adapter — discli Subprocess Bridge",
  "summary": "DiscliAdapter connects PocketPaw to Discord by spawning a discli serve subprocess and communicating with it over stdin/stdout JSONL, rather than using discord.py directly. This architecture isolates the Discord event loop in a separate process, preventing it from interfering with PocketPaw's asyncio loop, and lets discli handle rate limiting, reconnection, and slash command registration independently.",
  "concepts": [
    "DiscliAdapter",
    "discli serve",
    "subprocess JSONL bridge",
    "Discord",
    "streaming",
    "conversation history",
    "DISCORD_MSG_LIMIT",
    "_NO_RESPONSE_MARKER",
    "slash commands",
    "channel access control",
    "process isolation"
  ],
  "categories": [
    "channel-adapters",
    "discord",
    "message-bus",
    "streaming"
  ],
  "source_docs": [
    "0000000000000012"
  ],
  "backlinks": null,
  "word_count": 460,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Architecture: Process Bridge Pattern

Early versions of the Discord adapter used discord.py directly in-process. The problem: discord.py runs its own event loop, which conflicts with FastAPI's and asyncio's loops, causing subtle deadlocks in long-running sessions. The `discli serve` subprocess bridge solves this by moving the Discord event loop to a child process entirely.

Communication between PocketPaw and discli is via **stdin/stdout JSONL** (one JSON object per line). PocketPaw writes commands (send message, set status, stream update) as JSON lines to the process stdin. discli writes events (message received, reaction added, slash command invoked) as JSON lines to stdout. `DiscliAdapter` reads the stdout stream in a background asyncio task and converts events to `InboundMessage` objects on the PocketPaw bus.

## Conversation History

The adapter maintains a per-channel conversation history buffer (`_CONVERSATION_HISTORY_SIZE = 30` messages, `_CONVERSATION_CHAR_BUDGET = 12_000` characters). This provides the agent with recent context when a user sends a follow-up message in a Discord channel without explicitly re-stating their prior question. The history is trimmed to the character budget first, then the message count, so very long messages do not cause the budget to balloon.

## Streaming Support

Discord messages have a 2,000-character limit (`DISCORD_MSG_LIMIT`). For streaming agent responses, the adapter edits the in-progress message in place as new tokens arrive. The `_STREAM_BUFFER_THRESHOLD = 25` constant introduces a small buffer before the first edit: the adapter waits until it has received at least 25 characters before sending the initial message. This prevents a flurry of API calls for short responses and guards against the `[NO_RESPONSE]` marker being split across stream chunks.

## [NO_RESPONSE] Marker

Some agent responses are intentionally empty (the agent decided not to reply). The adapter checks for a `[NO_RESPONSE]` sentinel in the response stream and suppresses the Discord send entirely. Without this check, the bot would send an empty message to Discord — confusing to users and wasteful of API quota.

## Slash Commands

`_SIMPLE_SLASH_COMMANDS` is a frozen set of slash commands that map directly to internal PocketPaw commands without extra arguments (`/new`, `/clear`, `/status`, etc.). These are registered with discli at startup. Commands outside this set are handled by the agent loop as natural language.

## Channel Access Control

The adapter supports three levels of access restriction: `allowed_guild_ids`, `allowed_user_ids`, and `allowed_channel_ids`. Messages from outside these sets are silently dropped before being placed on the bus. Conversation mode (`conversation_channel_ids`, `conversation_all_channels`) controls which channels get the full conversation history context versus single-turn processing.

## Known Gaps

The `_IDLE_CHANNEL_TTL = 3600` seconds TTL for channel conversation history is not automatically enforced. Stale histories are only cleaned up when a new message arrives in an expired channel, meaning a channel that goes quiet for exactly one hour and then sees a burst of messages may experience a history-purge mid-conversation.