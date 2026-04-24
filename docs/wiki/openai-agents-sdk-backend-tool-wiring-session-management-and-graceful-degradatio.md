---
{
  "title": "OpenAI Agents SDK Backend: Tool Wiring, Session Management, and Graceful Degradation",
  "summary": "Tests for the OpenAI Agents SDK backend adapter, covering custom tool registration, per-backend provider selection, SDK-absent graceful degradation, native SQLite session management, and lazy database path resolution. All tests run without a real OpenAI Agents SDK installation via comprehensive mocking.",
  "concepts": [
    "OpenAI Agents SDK",
    "backend adapter",
    "tool wiring",
    "tool_bridge",
    "graceful degradation",
    "SQLiteSession",
    "lazy initialization",
    "per-backend provider",
    "Ollama",
    "session management",
    "capability map"
  ],
  "categories": [
    "agent backends",
    "OpenAI",
    "testing",
    "multi-turn",
    "test"
  ],
  "source_docs": [
    "45ea73a4ef06edf5"
  ],
  "backlinks": null,
  "word_count": 557,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's `OpenAIAgentsBackend` wraps the OpenAI Agents SDK to provide an alternative execution engine to the default Claude-based backend. The test file is structured so the full suite passes even without the SDK installed — a requirement because CI environments and offline machines should not need an OpenAI account to run tests.

## Custom Tool Wiring (`TestOpenAIAgentsCustomTools`)

PocketPaw has a set of built-in tools (memory, OCR, etc.) that must be registered with the OpenAI Agents SDK agent so the LLM can invoke them:

- `test_custom_tools_cached`: `_build_custom_tools()` calls `tool_bridge.build_openai_function_tools()` once and caches the result. Rebuilding on every agent creation would be wasteful and could cause subtle ordering differences if the tool list is dynamically generated.
- `test_custom_tools_graceful_degradation`: If `tool_bridge` is unavailable (import error), the method returns an empty list rather than raising. An agent with no custom tools is degraded but functional; an agent that crashes on startup is not.
- `test_agent_created_with_tools`: When `run()` is called, the underlying SDK agent is constructed with the custom tools. The test captures the `Agent(...)` constructor call and asserts the `tools` argument is non-empty.

## Backend Info and Policy (`TestOpenAIAgentsInfo`)

- `test_info_static`: Static metadata (name, display name) is correct. This is consumed by the UI's backend selector.
- `test_tool_policy_map`: The policy map declares which PocketPaw tools the backend supports natively vs. which need the tool bridge. An incorrect policy would silently disable tools.
- `test_required_keys_and_providers`: Lists the API key names and provider identifiers required for this backend. Used by the configuration validator to give early errors when credentials are missing.

## Per-Backend Provider (`TestOpenAIAgentsProvider`)

The OpenAI Agents backend can be pointed at different OpenAI-compatible endpoints via a `openai_agents_provider` setting, separate from the global `llm_provider`:

- `test_build_model_uses_per_backend_provider`: When `openai_agents_provider` is set, it takes precedence over the global provider.
- `test_build_model_ollama_via_per_backend_provider`: Ollama can be used as the model backend for OpenAI Agents SDK by setting `openai_agents_provider="ollama"`. This enables fully local multi-turn agent sessions.
- `test_build_model_falls_back_to_llm_provider`: If `openai_agents_provider` is not set, the global `llm_provider` is used. Backward compatibility is preserved.

## SDK-Absent Behavior (`TestOpenAIAgentsInit`)

- `test_init_without_sdk`: The backend class can be instantiated even when `openai_agents` is not installed. Import errors are caught and deferred until `run()` is called.
- `test_run_without_sdk`: Calling `run()` without the SDK installed raises a clear error with installation instructions, not an `ImportError` with a raw traceback.
- `test_stop` / `test_get_status`: These methods must work (returning safe defaults) even when the SDK is absent and no sessions have been started.

## Lazy Database Path (`TestSessionDBPathLazy`)

- `test_session_db_path_resolves_lazily`: The SQLite session database path (`_SESSION_DB`) is resolved at first use, not at module import time. If it were resolved at import, `import pocketpaw.agents.openai_agents` would fail in environments where the home directory is not yet configured (e.g., certain container setups). The test confirms the module imports cleanly without touching the filesystem.

## Session Management (`TestOpenAIAgentsSessions`)

The backend uses the SDK's native `SQLiteSession` for multi-turn conversation history:

- `test_session_created_for_key`: A new session is created for each unique session key (typically a conversation ID).
- `test_different_keys_get_different_sessions`: Two different keys must produce different `SQLiteSession` instances, preventing conversation bleed between users.

## Known Gaps

No TODOs in the file. The tests mock the entire OpenAI Agents SDK, so actual SDK behavior (streaming, tool call formatting, session serialization format) is not exercised here. Behavioral compatibility with the real SDK is assumed to be validated in integration or E2E tests.
