---
{
  "title": "OpenCode Backend: REST API Integration, Session Caching, and Tool Injection",
  "summary": "Tests for the OpenCodeBackend, which connects PocketPaw to OpenCode's REST API for agentic coding tasks. Covers health checking, session creation and caching, the message endpoint contract, system prompt tool instruction injection, streaming responses, and graceful error handling for unreachable servers.",
  "concepts": [
    "OpenCodeBackend",
    "REST API",
    "session management",
    "health check",
    "message endpoint",
    "tool injection",
    "system prompt",
    "streaming",
    "httpx",
    "session caching",
    "capability declaration"
  ],
  "categories": [
    "agent backends",
    "REST",
    "testing",
    "coding agents",
    "test"
  ],
  "source_docs": [
    "3f348b662bca152a"
  ],
  "backlinks": null,
  "word_count": 564,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`OpenCodeBackend` lets PocketPaw delegate coding tasks to a running OpenCode instance via its REST API. Unlike the SDK-based backends, OpenCode is an external process with its own session state, so the backend must manage HTTP sessions, handle connection failures gracefully, and inject PocketPaw's tool instructions into every request.

## Static Info (`TestOpenCodeInfo`)

- `test_info_name` / `test_info_display_name`: The backend identifies as `"opencode"` / `"OpenCode"`. These strings appear in the UI's backend selector.
- `test_info_capabilities`: The backend supports `STREAMING`, `TOOLS`, `MULTI_TURN`, and `CUSTOM_SYSTEM_PROMPT`. The capability list gates feature availability — declaring a capability incorrectly would enable UI elements that fail at runtime.
- `test_info_no_builtin_tools`: OpenCode handles tools itself; PocketPaw should not inject its tool bridge. The empty `builtin_tools` and `tool_policy_map` confirm this separation.

## Initialization (`TestOpenCodeInit`)

- `test_default_base_url`: Without configuration, the backend connects to `http://localhost:4242` — OpenCode's default port.
- `test_custom_base_url`: A configured `opencode_base_url` overrides the default. Operators running OpenCode on a different port or host need this.
- `test_session_map_empty_on_init`: No sessions are pre-created at startup; they are created lazily on first use. This avoids slow initialization when OpenCode is not running.

## Health Checks (`TestOpenCodeHealth`)

The health endpoint lets PocketPaw confirm OpenCode is running before attempting inference:

- `test_health_success`: A 200 response means healthy.
- `test_health_server_error`: A 5xx response returns unhealthy rather than raising — the backend handles HTTP errors gracefully.
- `test_health_connect_error`: Connection refused (OpenCode not running) returns unhealthy with a descriptive message.
- `test_health_timeout`: Request timeout also returns unhealthy. Without timeout handling, a slow OpenCode server would hang the entire PocketPaw request.

## Session Management (`TestOpenCodeSession`)

OpenCode uses server-side sessions for multi-turn conversations. PocketPaw creates and caches them:

- `test_create_session`: A `POST /session` call creates a session and returns an ID.
- `test_session_cached`: The session ID is cached per conversation key. Without caching, every message would create a new session, losing conversation history.

## Run and Message Endpoint (`TestOpenCodeRun`)

The class docstring explicitly documents that the backend uses `POST /session/{id}/message`, not `POST /prompt`. This was a discovered contract difference that caused failures before the test was added.

- `test_run_uses_message_endpoint`: Asserts the POST goes to `/session/{id}/message`. If the endpoint changes, this test catches it before deployment.
- `test_run_text_response` / `test_run_tool_response`: Normal text responses and tool-call responses are parsed and yielded as `AgentEvent` objects.
- `test_run_with_system_prompt`: The system prompt is included in the request payload.
- `test_run_with_model`: A custom model name overrides the default in the request.
- `test_run_http_error`: HTTP errors during a run are caught and surfaced as error events rather than raising, maintaining the streaming protocol.
- `test_run_server_unreachable`: If OpenCode goes offline mid-run, a connection error yields an error event.

## Tool Instruction Injection (`TestOpenCodeToolInstructions`)

Since OpenCode manages its own tool execution, PocketPaw cannot register tools directly. Instead, it appends a tool instructions section to the system prompt describing available PocketPaw tools:

- `test_system_payload_includes_tool_instructions`: The tool section is appended after the user-supplied system prompt.
- `test_tool_section_appended_without_system_prompt`: Even without a user system prompt, the tool instructions are injected. This ensures PocketPaw tools are always discoverable by the OpenCode agent.

## Stop and Status (`TestOpenCodeStop`)

- `test_stop_sets_flag`: Calling `stop()` sets an internal flag that the run loop checks to gracefully cancel streaming.

## Known Gaps

No explicit TODOs. The tests mock `httpx.AsyncClient` rather than a real OpenCode server, so OpenCode's actual response format (JSON schema, streaming protocol) is assumed but not validated. Changes to OpenCode's API would not be caught until integration testing.
