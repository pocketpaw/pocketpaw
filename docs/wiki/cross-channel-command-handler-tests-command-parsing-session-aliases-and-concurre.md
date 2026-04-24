---
{
  "title": "Cross-Channel Command Handler Tests: Command Parsing, Session Aliases, and Concurrent Write Safety",
  "summary": "This test suite validates PocketPaw's slash-command system and session alias manager, which allow users on any channel (Discord, Telegram, WhatsApp, CLI) to issue commands like `/new`, `/sessions`, `/resume`, and `/help`. Tests cover command recognition, alias CRUD operations, concurrent alias write safety, and the full command execution flow.",
  "concepts": [
    "is_command",
    "session aliases",
    "concurrent writes",
    "asyncio.Lock",
    "slash commands",
    "Telegram bot suffix",
    "resume",
    "new session",
    "sessions list",
    "FileMemoryStore"
  ],
  "categories": [
    "commands",
    "testing",
    "session management",
    "concurrency",
    "multi-channel",
    "test"
  ],
  "source_docs": [
    "d40bab22b9da0a2f"
  ],
  "backlinks": null,
  "word_count": 450,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports a set of meta-commands that users can issue from any messaging channel. These commands (prefixed with `/` and optionally the bot's name) manage conversation sessions: starting new sessions, listing existing ones, and resuming a named session. The command handler is channel-agnostic — it parses the raw message content and dispatches to the appropriate operation. Session aliases provide human-readable names for session keys, stored as a JSON file on disk.

## Command Recognition — `is_command()`

```python
class TestIsCommand:
    def test_recognises_new(self):              # "/new" -> True
    def test_handles_bot_suffix(self):           # "/new@PocketPawBot" -> True
    def test_case_insensitive(self):             # "/NEW" -> True
    def test_leading_whitespace(self):           # " /new" -> True
    def test_rejects_unknown_command(self):      # "/unknown" -> False
    def test_rejects_plain_text(self):           # "hello" -> False
```

The `@BotName` suffix handling is critical for Telegram group chats, where commands must be addressed to the specific bot (e.g., `/new@PocketPawBot`) to distinguish between multiple bots in the same group. Case insensitivity and whitespace trimming prevent user frustration from minor formatting differences.

## Session Alias CRUD

```python
class TestSessionAliases:
    async def test_set_and_resolve(self):
        # alias "home" resolves to the session key it was mapped to

    async def test_aliases_persist_to_disk(self):
        # alias file is written and readable after set

    async def test_get_session_keys_includes_alias_targets(self):
        # the session's key appears in the full list of session keys
```

Aliases are stored as a JSON file rather than in-memory to survive PocketPaw restarts. The `test_get_session_keys_includes_alias_targets` test verifies that alias target session keys are visible in the sessions list — without this, users could create aliases to sessions that the `/sessions` command would never show.

## Concurrent Alias Write Safety

```python
async def test_concurrent_alias_writes(self):
    async def _write(i):
        await aliases.set_alias(f"alias{i}", f"session{i}")
    await asyncio.gather(*[_write(i) for i in range(10)])
    # all 10 aliases are present and correct
```

This test addresses a real race condition: the alias store reads the JSON file, modifies the dict, and writes it back. If two concurrent writes both read the file before either writes, one write will overwrite the other's changes. The test runs 10 concurrent writes and verifies all 10 aliases survive. The fix typically involves an `asyncio.Lock` around the read-modify-write cycle.

## `/new` Command

```python
class TestNewCommand:
    async def test_new_creates_alias(self, mock_get_mm):
        msg = _make_msg("/new my-session")
        # Creates a new session and sets alias "my-session"

    async def test_new_with_bot_suffix(self, mock_get_mm):
        msg = _make_msg("/new@PocketPawBot my-session")
        # Still works with Telegram bot suffix
```

The `/new` command creates a fresh conversation session and optionally assigns an alias for easy resumption. The bot-suffix test is duplicated here to ensure the full command execution path handles the Telegram format.

## Known Gaps

No test covers what happens when the alias file is corrupted (invalid JSON). The behavior when resuming a non-existent alias is not covered in the visible test cases.