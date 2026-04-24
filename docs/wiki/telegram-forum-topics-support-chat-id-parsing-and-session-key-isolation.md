---
{
  "title": "Telegram Forum Topics Support: Chat ID Parsing and Session Key Isolation",
  "summary": "Tests for Telegram Group Topics (forum supergroups) support in the PocketPaw Telegram adapter, covering the `_parse_chat_id` utility that splits composite chat IDs into chat and topic components, and session key generation that ensures different forum topics produce different session keys.",
  "concepts": [
    "TelegramAdapter",
    "forum topics",
    "chat ID parsing",
    "topic ID",
    "session key isolation",
    "_parse_chat_id",
    "supergroup",
    "message_thread_id",
    "session routing"
  ],
  "categories": [
    "testing",
    "channel adapters",
    "Telegram",
    "test"
  ],
  "source_docs": [
    "97f987686378635d"
  ],
  "backlinks": null,
  "word_count": 445,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Telegram's Group Topics feature (available in supergroups configured as forums) allows a single group chat to host multiple named topic threads. Each topic is a separate conversation context. PocketPaw's Telegram adapter must route messages to the correct agent session based on both the chat ID and the topic ID. This test file was created on 2026-02-07 to pin that routing logic.

## Why This Matters

Without topic-aware session keys, a message in Topic A and a message in Topic B within the same forum group would land in the same agent session. Users in different topics would see each other's conversation history, the agent would mix up context, and multi-topic workflows would be impossible.

## Chat ID Format

PocketPaw represents a Telegram destination as a string with an optional topic suffix: `"<chat_id>"` for standard chats and `"<chat_id>:topic:<topic_id>"` for forum topics. The `_parse_chat_id` static method parses this format.

`TestParseChatId` covers four variants:

- `test_plain_chat_id`: `"123456"` → `chat_id="123456"`, `topic_id=None`. No topic means standard group or DM.
- `test_topic_chat_id`: `"123456:topic:42"` → `chat_id="123456"`, `topic_id=42`. Standard forum topic.
- `test_topic_id_zero`: `"123456:topic:0"` is a valid edge case — topic ID 0 is Telegram's "General" topic. The parser must treat `0` as a valid topic ID, not as a falsy sentinel.
- `test_negative_chat_id_with_topic`: `"-100123456:topic:7"` handles supergroup chat IDs, which are always negative in Telegram's API. The parser must not confuse the negative sign with a parsing error.

The `topic_id_zero` test is the defensive edge case. A naive implementation might check `if topic_id:` and treat 0 as "no topic", sending General topic messages to the wrong session.

## Session Key Generation

`TestTopicSessionKey` verifies the downstream effect of topic parsing: that the session key used by the agent includes the topic ID.

- `test_forum_message_includes_topic`: A message update with `is_topic_message=True` and a non-zero `message_thread_id` produces a session key that encodes the topic. The exact format is not specified in the test names, but the key must differ from a non-topic message's key.
- `test_non_forum_message_no_topic`: A standard (non-forum) message produces a session key without a topic component.
- `test_different_topics_different_session_keys`: Two messages in the same chat but different topics produce different session keys. This is the core correctness test — it directly verifies session isolation between topics.

## Known Gaps

The test suite covers parsing and session key generation but does not test the downstream routing: confirming that two agents are actually running in separate sessions when the same chat has two active topics. That would require an integration test with a running `AgentLoop`.

```python
# Parse chat ID examples
chat_id, topic_id = TelegramAdapter._parse_chat_id("123456")
# -> ("123456", None)

chat_id, topic_id = TelegramAdapter._parse_chat_id("123456:topic:42")
# -> ("123456", 42)

chat_id, topic_id = TelegramAdapter._parse_chat_id("-100123456:topic:7")
# -> ("-100123456", 7)
```
