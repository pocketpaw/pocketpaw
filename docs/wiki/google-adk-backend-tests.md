---
{
  "title": "Google ADK Backend Tests",
  "summary": "This module provides fully mocked tests for `GoogleADKBackend`, PocketPaw's Google Agent Development Kit integration. It covers metadata/capability discovery, initialization with and without the SDK, event stream processing (text, function calls, function responses, errors), session management, MCP toolset policy enforcement, status reporting, and the backward-compatibility alias `GeminiCLIBackend`.",
  "concepts": [
    "GoogleADKBackend",
    "GeminiCLIBackend",
    "google-adk",
    "BackendInfo",
    "session management",
    "MCP toolsets",
    "tool policy",
    "stop flag",
    "max turns",
    "event stream",
    "function calls",
    "function responses",
    "history seeding"
  ],
  "categories": [
    "testing",
    "agent backend",
    "Google integrations",
    "test"
  ],
  "source_docs": [
    "4091d2b7a24951fe"
  ],
  "backlinks": null,
  "word_count": 522,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Mocking Is Required

`google-adk` is an optional dependency with GPU/cloud requirements. Tests use `SimpleNamespace` to fabricate ADK event objects and patch `sys.modules` to inject mock Google library modules. This means CI can run without any Google credentials or the SDK installed.

## Backend Info and Capabilities (TestGoogleADKInfo)

Three tests lock in the `BackendInfo` metadata:

- `info.name == "google_adk"`, `info.display_name == "Google ADK"`.
- Capabilities include `STREAMING`, `TOOLS`, `MCP`, `MULTI_TURN`, and `CUSTOM_SYSTEM_PROMPT`.
- Built-in tools `google_search` and `code_execution` are declared, with policy map entries mapping them to `"browser"` and `"shell"` trust groups respectively.
- Install hint specifies `pip_package="google-adk"`, `pip_spec="pocketpaw[google-adk]"`, and `verify_import="google.adk"`.
- Required keys include `google_api_key`.

## Initialization (TestGoogleADKInit)

- **With SDK** — `@patch.dict(sys.modules, {"google.adk": MagicMock()})` injects a mock SDK, and `backend._sdk_available` should be `True`.
- **Without SDK** — constructing via `__new__` and manually setting `_sdk_available = False` simulates a missing dependency.
- **Run without SDK** — yields exactly one event with `type == "error"` and `"not installed"` in the content, so the agent can surface an actionable installation message.

## Event Helpers

Three factory functions produce `SimpleNamespace` objects that mirror the ADK event structure:

```python
def _make_text_event(text, author="PocketPaw"):
    part = SimpleNamespace(text=text, function_call=None, function_response=None)
    content = SimpleNamespace(role="model", parts=[part])
    return SimpleNamespace(author=author, content=content)
```

## Run Event Processing (TestGoogleADKRun)

- **Text events** — two sequential text events produce two `message` type events in the output stream.
- **Function call events** — a `function_call` part produces a `tool_use` event with `metadata["name"]` and `metadata["input"]`.
- **Function response events** — a `function_response` part produces a `tool_result` event.
- **Error handling** — when `run_async` raises `RuntimeError`, the backend yields an `error` event containing `"ADK"` in the message.
- **Stop flag** — setting `backend._stop_flag = True` mid-iteration halts event consumption after the first message.
- **Max turns** — `google_adk_max_turns=2` causes an error event with `"Max turns"` after two tool calls, preventing runaway agentic loops.

## Session Management (TestGoogleADKSessions)

- **New session** — a new `session_key` triggers `create_session` and is stored in `backend._sessions`.
- **Session reuse** — an existing session key reuses the stored session ID without calling `create_session` again.
- **History seeding** — when a new session is started with `history=[...]`, the `system_prompt` (instruction) passed to the runner includes `"Recent Conversation"` and the history content. This allows the ADK to pick up where another backend left off.

## MCP Toolset Policy (TestGoogleADKMCP)

- **No deps** — `_build_mcp_toolsets` returns `[]` when MCP libs cannot be imported.
- **No config** — returns `[]` when no MCP servers are configured.
- **Policy blocks server** — `tools_deny=["mcp:blocked_server:*"]` excludes `blocked_server` from toolsets while allowing `allowed_server`. Only one `McpToolset` is instantiated.
- **Group deny** — `tools_deny=["group:mcp"]` blocks all MCP servers regardless of name.

## Status and Backward Compatibility

- `get_status()` returns a dict with `backend`, `available`, `active_sessions`, and `model`.
- `stop()` sets `_stop_flag = True`.
- `GeminiCLIBackend is GoogleADKBackend` — the alias is preserved for backward compatibility with settings that use the old name.

## Known Gaps

No tests cover OAuth token refresh for `google_api_key`, the actual Pub/Sub polling path, or multi-modal input events (images, audio). The `_get_runner` method that creates the real ADK runner is patched in all tests and not directly tested.