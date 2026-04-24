---
{
  "title": "Telegram Channel Adapter Tests: Typing Indicators, Smart Buffering, and Voice Routing",
  "summary": "Tests for PocketPaw's Telegram adapter covering typing indicator lifecycle, smart stream buffering with rate limiting, markdown formatting on flush, voice media routing, and the complete streaming cycle from first chunk to final message. The `python-telegram-bot` library is mocked as an optional dependency.",
  "concepts": [
    "TelegramAdapter",
    "typing indicator",
    "stream buffering",
    "rate limiting",
    "markdown formatting",
    "voice media routing",
    "forum topics",
    "python-telegram-bot",
    "ChatAction.TYPING"
  ],
  "categories": [
    "testing",
    "channel adapters",
    "Telegram",
    "test"
  ],
  "source_docs": [
    "9d4095c0fd6561c1"
  ],
  "backlinks": null,
  "word_count": 534,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's Telegram adapter provides a particularly rich UX: it shows typing indicators while the agent thinks, buffers streamed tokens and edits a single message in place (avoiding message spam), and routes voice responses through Telegram's audio message API. This test file was created on 2026-03-06 and systematically validates each of these behaviours.

## Mock Infrastructure

`python-telegram-bot` is mocked at `sys.modules` level before the adapter is imported. Two fixtures provide clean adapter instances:

- `adapter()`: A bare adapter with a test token and allowed user ID.
- `adapter_with_app(adapter)`: The same adapter with a fully mocked Telegram `Application` object for tests that exercise API calls.

## Adapter Defaults

`TestTelegramAdapterInit` verifies three things: configuration defaults match documentation, custom config overrides are applied, and timing constants (the intervals for rate limiting and typing indicator refresh) are defined as module-level constants rather than magic numbers. Constant-based timing makes it easy to adjust UX behaviour without hunting for raw numbers.

## Typing Indicator Lifecycle

`TestTypingIndicator` tests the complete typing indicator state machine:

- `test_send_typing_indicator`: Calls `send_chat_action(ChatAction.TYPING)`.
- `test_send_typing_indicator_with_topic`: Passes `message_thread_id` for forum topics.
- `test_send_typing_indicator_no_app`: Silently does nothing if the app is not initialised.
- `test_send_typing_indicator_exception_caught`: API errors are caught, not propagated.
- `test_start_typing_indicator_creates_task`: Starts a background loop that keeps sending typing actions.
- `test_start_typing_indicator_idempotent`: Calling start twice does not create a second task — prevents typing indicator loops from multiplying.
- `test_stop_typing_indicator_cancels_task`: The background task is cancelled on stop.
- `test_stop_typing_indicator_nonexistent`: Stopping a non-existent session's indicator does not raise.

The idempotency test is the defensive one — without it, a code path that starts the indicator multiple times would create multiple background tasks, each sending `TYPING` actions concurrently and potentially hitting rate limits.

## Stream Buffering

`TestStreamBuffering` validates the "edit in place" streaming strategy:

- First chunk sends a placeholder message and starts typing.
- Subsequent chunks accumulate in the buffer.
- Rate-limited update: an edit call is made after the rate limit interval elapses.
- No update within the rate limit window: buffer accumulates without an API call.
- Stream end flushes the buffer with the complete final text.
- Flush applies Markdown formatting to the final message.

The rate limiting is essential — Telegram's API enforces strict message edit rate limits (roughly 1 edit per second per chat). Without buffering and throttling, streaming would trigger `TooManyRequests` errors.

## Streaming Lifecycle Integration

`TestStreamingLifecycle` chains all stream events together in sequence and confirms the final message contains the complete assembled text.

## Stop Cleanup

`TestOnStop` verifies that `_on_stop` cancels all active typing indicator tasks across all sessions, not just the current one. This is essential for clean shutdown when multiple users are interacting concurrently.

## Message Update Edge Cases

Three `TestMessageUpdate` tests cover empty text suppression, exception handling during updates, and topic-aware message editing.

## Voice Media Routing

`TestVoiceMediaRouting` tests the `_is_voice_media` detection logic and confirms that detected voice payloads are sent via `send_audio` (or fall back to audio attachment), not as text messages.

## Known Gaps

No tests for the long-polling vs webhook mode distinction or for the user ID allowlist enforcement on inbound messages.

```python
# Rate limiting pattern for stream updates
if time.monotonic() - self._last_edit_time >= EDIT_RATE_LIMIT:
    await self._update_message(session_key, self._stream_buffer[session_key])
    self._last_edit_time = time.monotonic()
```
