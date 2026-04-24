---
{
  "title": "OpenAI Agents SDK Backend — GPT and Ollama Agent Support with SQLite Session Persistence",
  "summary": "Implements `OpenAIAgentsBackend` using OpenAI's `openai-agents` SDK, supporting GPT models and any OpenAI-compatible endpoint (Ollama, OpenRouter, LiteLLM). Sessions are persisted to a SQLite database so multi-turn conversations survive process restarts, unlike most other backends.",
  "concepts": [
    "OpenAIAgentsBackend",
    "openai-agents",
    "SQLiteSession",
    "session persistence",
    "Ollama",
    "OpenAI-compatible",
    "FunctionTool",
    "history injection",
    "code_interpreter",
    "computer_use",
    "lazy path resolution"
  ],
  "categories": [
    "agent-runtime",
    "openai",
    "session-management",
    "local-models"
  ],
  "source_docs": [
    "88ba94cf501bab60"
  ],
  "backlinks": null,
  "word_count": 403,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`OpenAIAgentsBackend` integrates OpenAI's `openai-agents` SDK into PocketPaw, supporting GPT models and any OpenAI-compatible endpoint including local models via Ollama. Unlike most other backends, it persists conversation history to SQLite, so multi-turn context survives PocketPaw restarts.

## SQLite Session Persistence

`SQLiteSession` stores conversation turns in `~/.pocketpaw/openai_agents_sessions.db`, keyed by the PocketPaw `session_key`. This is the only backend in PocketPaw that provides durable multi-turn memory across process restarts. The other backends all hold context in-memory and lose it when PocketPaw restarts.

`_get_session_db_path()` resolves the path lazily so `Path.home()` is evaluated at call time, not at module import. This matters in container environments and test suites where `HOME` may not be set at import time.

## OpenAI-Compatible Local Model Support

`_build_model()` detects `settings.provider`:
- `ollama` → constructs `OpenAIChatCompletionsModel` with the local Ollama base URL and no API key requirement
- `openai_compatible` / `openrouter` / `litellm` → uses a custom base URL with the configured API key
- `openai` → uses the SDK's standard model setup

This makes `OpenAIAgentsBackend` a viable path for privacy-sensitive deployments running entirely on local hardware.

## Custom Tool Registration

`_build_custom_tools()` wraps PocketPaw tools as SDK `FunctionTool` objects. Tool schemas are derived from PocketPaw's tool registry, so any tool registered with PocketPaw becomes available to the OpenAI agent without modifications.

## History Injection Fallback

The SDK's SQLite session handles history natively, but `_inject_history()` prepends prior turns into the instruction when the session key is new. This handles the edge case where conversation history exists in PocketPaw's memory but the SQLite DB was cleared or moved.

## Defensive Tool Name Extraction

`_extract_tool_name()` handles both `ToolCallItem` objects (SDK v1 style) and plain dicts (some SDK versions serialise tool calls differently). This prevents crashes when users run a different patch version of `openai-agents` than PocketPaw was developed against.

## Built-in Tool Policy Mapping

| SDK Tool | Policy Category |
|----------|----------------|
| `code_interpreter` | `shell` |
| `file_search` | `read_file` |
| `computer_use` | `shell` |

## Known Gaps

- `computer_use` is listed but requires a specific model (CUA-compatible) and extra provisioning; enabling without proper setup causes SDK errors.
- No `Capability.MCP` support.
- SQLite DB path is not configurable via PocketPaw settings.


## Session-Level Granularity

Each `session_key` maps to a separate `SQLiteSession` row. Multiple conversations from the same user on different days each get independent session history. This granularity matches PocketPaw's session model where a `session_key` identifies a single conversation thread, not a user.
