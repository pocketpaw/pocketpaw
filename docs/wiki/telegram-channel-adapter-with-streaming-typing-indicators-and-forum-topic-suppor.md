---
{
  "title": "Telegram Channel Adapter with Streaming, Typing Indicators, and Forum Topic Support",
  "summary": "TelegramAdapter connects PocketPaw to the Telegram Bot API using python-telegram-bot, supporting streaming responses via live message edits, animated typing indicators, media file sending, voice note detection, and Telegram forum topic (Supergroup) thread routing.",
  "concepts": [
    "python-telegram-bot",
    "typing indicator",
    "streaming via message edits",
    "edit_message_text",
    "forum topics",
    "message_thread_id",
    "voice notes",
    "send_voice",
    "send_audio",
    "Markdown fallback",
    "allowed_user_id",
    "long polling",
    "BaseChannelAdapter"
  ],
  "categories": [
    "channel-adapters",
    "messaging",
    "telegram",
    "streaming"
  ],
  "source_docs": [
    "f941d70159573d0e"
  ],
  "backlinks": null,
  "word_count": 542,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

TelegramAdapter is one of the most fully featured adapters in PocketPaw. It uses `python-telegram-bot`'s `Application` class with long-polling to receive updates, and exposes a rich sending interface that includes live-edited streaming responses, typing indicator management, media uploads, and Telegram forum topic support.

## Typing Indicator Refresh Loop

Telegram clears the "typing..." status after approximately 5 seconds if not refreshed. The adapter runs a per-chat background task (`_typing_loop`) that calls `send_chat_action(ChatAction.TYPING)` every 4 seconds (`_TYPING_REFRESH_INTERVAL`). The task is created in `_start_typing_indicator()` and cancelled in `_stop_typing_indicator()`, which is called when `stream_end` arrives. An idempotency guard prevents duplicate tasks for the same chat.

## Streaming via Message Edits

When the agent starts streaming, `_handle_stream_chunk()` sends an initial `"🧠 ..."` placeholder message and stores its `message_id` in `_buffers`. Subsequent chunks are accumulated in the buffer and the message is updated via `edit_message_text()` no more than once every 1.5 seconds (`_BUFFER_UPDATE_INTERVAL`) to stay within Telegram's rate limits. On `stream_end`, `_flush_stream_buffer()` issues the final edit with the complete formatted text.

Note: all mid-stream edits use `parse_mode=None` because partial Markdown (e.g., an unclosed `*`) would cause parse errors that silently drop the message. The final flush applies `convert_markdown()` after the text is complete.

## Markdown Fallback

For non-streamed messages, `send_message()` is called with `parse_mode="Markdown"`. If this raises an exception (common when the text contains unescaped `_` characters inside code identifiers), the adapter retries with `parse_mode=None` and the raw unformatted text. This prevents complete message delivery failure due to formatting issues.

## Forum Topic Routing

Telegram Supergroups with "Topics" enabled assign a `message_thread_id` to each thread. The adapter encodes this as a `chat_id` suffix: `"{chat_id}:topic:{thread_id}"`. All send methods call `_parse_chat_id()` to split this compound key before constructing API calls, injecting `message_thread_id` into kwargs when present. This allows PocketPaw to maintain separate conversation sessions per forum topic within the same Supergroup.

## Voice Note Detection

Audio files can be sent as either `send_audio` (shows waveform and title) or `send_voice` (shown as a voice note bubble). The adapter determines which to use via `_is_voice_media()`, which checks `OutboundMessage.metadata` for `is_voice: true` or a `voice_media_paths` list. Voice hints are cached in `_voice_media_hints` keyed by `(chat_id, path)` and evicted after 1 hour (`_VOICE_MEDIA_HINT_TTL`) to prevent unbounded memory growth.

If `send_voice` fails (e.g., wrong MIME type), the adapter falls back to `send_audio` by re-opening the file.

## Inbound Media Handling

`_handle_message()` downloads the highest-resolution variant for photos (always `photo[-1]`), and handles documents, audio, video, voice, and video_note types. Each file is downloaded with `file_obj.download_as_bytearray()` and saved via `MediaDownloader.save_from_bytes()`. The filename and MIME type extracted from the Telegram message object are preserved.

## Authorization

`allowed_user_id` restricts all inbound interactions to a single Telegram user ID. Requests from any other user are silently dropped (for messages) or replied with "Unauthorized" (for `/start`).

## Bot Command Menu

On startup, `set_my_commands()` registers the full PocketPaw command set with Telegram's Bot API, enabling autocomplete in the Telegram UI. Registration failure is caught and logged as a warning, not an error, since it does not affect core functionality.

## Known Gaps

- Only one `allowed_user_id` is supported. Multi-user or group-level authorization requires extending the filtering logic.
- Streaming edits over Telegram's rate limit silently skip updates; in very high-throughput scenarios, users may see the response jump forward without intermediate states.