---
{
  "title": "Cross-Channel Slash Command Handler",
  "summary": "CommandHandler intercepts slash commands from any channel (Telegram, Slack, WhatsApp, CLI, etc.) and processes them directly without invoking the AI agent backend. It manages session lifecycle operations — creating, listing, resuming, renaming, and deleting conversation sessions — as well as runtime configuration of backend, model, and tool settings.",
  "concepts": [
    "CommandHandler",
    "slash commands",
    "session management",
    "session alias",
    "runtime configuration",
    "_normalize_cmd",
    "is_command",
    "kill switch",
    "settings changed callback",
    "agent loop cancellation",
    "session_key",
    "cross-channel"
  ],
  "categories": [
    "bus",
    "commands",
    "session-management",
    "configuration"
  ],
  "source_docs": [
    "bace25d620e45ac1"
  ],
  "backlinks": null,
  "word_count": 536,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

CommandHandler implements a unified command dispatch layer that sits between channel adapters and the agent loop. When a message matches a known command (e.g., `/new`, `/sessions`, `/backend claude_agent_sdk`), the bus routes it to `CommandHandler.handle()` rather than the AI backend. This short-circuits the expensive LLM call and returns an immediate, deterministic response.

## Command Recognition and Normalization

Commands are detected by `_CMD_RE`, a regex that matches both `/cmd` and `!cmd` prefixes with an optional `@BotName` suffix (common on Telegram). The `_normalize_cmd()` function converts `!cmd` to `/cmd` so all downstream logic is prefix-agnostic. The `!` fallback exists because some chat clients (notably Matrix/Element) intercept unknown `/` commands client-side and never deliver them to the bot.

The `is_command()` method provides a fast pre-check used by the bus before routing, avoiding a full dispatch cycle for regular chat messages.

## Session Management Commands

- **`/new`** — creates a UUID-suffixed session key alias via `memory.set_session_alias()`, preserving all previous sessions
- **`/sessions`** — lists all sessions for the current chat with message counts and active state; caches the result in `_last_shown` per `session_key` so `/resume <n>` can reference sessions by their display index without a second database query
- **`/resume <n|text>`** — switches to a session by numeric index (from the last `/sessions` list) or by fuzzy-matching the session title
- **`/clear`** — resets message history for the current session
- **`/rename <title>`** — updates the session's display name
- **`/status`** — returns backend, model, session title, message count, and active tool profile in a single response
- **`/delete`** — removes the current session and its messages; suggests `/new` to start fresh

## Runtime Configuration Commands

- **`/backends`** — lists all configured backend names with `(active)` marker
- **`/backend <name>`** — switches the active AI backend; calls `_notify_settings_changed()` to propagate the change to listeners (e.g., the agent loop that caches the backend)
- **`/model [name]`** — shows or updates the model for the current backend using `_BACKEND_MODEL_FIELDS` to map backend names to their settings field
- **`/tools [profile]`** — shows or switches the active tool profile; also notifies settings changed

## Kill Switch

`/kill` calls `self._agent_loop.cancel_current_run(session_key)` (if an agent loop is registered) to interrupt an in-progress agent run for the current session. This lets users abort runaway or stuck agent calls without restarting the server.

## Settings Change Callback

Mutating commands (`/backend`, `/model`, `/tools`) invoke `_notify_settings_changed()`, which fires a registered callback. This decoupling allows the agent loop — which caches the active backend — to reload settings without polling or restart.

## Response Formatting

All command responses are plain `OutboundMessage` objects with `channel` and `chat_id` mirrored from the inbound message. No Markdown formatting hints are applied — the `convert_markdown()` call happens downstream in the sending adapter. Command responses are intentionally concise to work well on narrow-display channels like WhatsApp.

## Global Singleton

`get_command_handler()` returns a module-level singleton. This means the `_last_shown` session cache and agent loop reference are shared across all concurrent requests, which is safe because `session_key` namespaces the cache per user.

## Known Gaps

- `/resume` fuzzy search is by substring match of session title only; there is no relevance ranking for ambiguous matches.
- Command responses do not yet support localization — all strings are English only.