---
{
  "title": "Test Suite for A2A Protocol Live Smoke Tests",
  "summary": "This script is a live, end-to-end smoke test suite for PocketPaw's A2A (Agent-to-Agent) protocol endpoints. It requires a running PocketPaw server with `POCKETPAW_A2A_ENABLED=true` and exercises every major A2A feature — agent card discovery, message sending, task management, streaming, cancellation, and error handling — reporting PASS/FAIL for each check.",
  "concepts": [
    "A2A protocol",
    "smoke test",
    "JSON-RPC",
    "agent card",
    "SSE streaming",
    "task cancellation",
    "terminal guard",
    "output mode",
    "live testing",
    "httpx",
    "POCKETPAW_A2A_ENABLED"
  ],
  "categories": [
    "testing",
    "a2a",
    "scripts",
    "integration",
    "test"
  ],
  "source_docs": [
    "932c2108d3207556"
  ],
  "backlinks": null,
  "word_count": 574,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`scripts/a2a_smoke_test.py` exists because A2A is a stateful, multi-step protocol that cannot be adequately tested with unit tests alone. Each feature (streaming, task cancel, JSON-RPC error codes) requires a live HTTP server to exercise the full request/response cycle. The script is designed to run manually by developers before releasing A2A changes, and by CI agents with access to a running PocketPaw instance.

## Test Structure

The script is not a pytest test — it is a standalone CLI program that maintains counters `passed` and `failed` and prints `PASS` / `FAIL` lines. This design was chosen because pytest's async fixtures and timeout handling added complexity that was not worth it for a live smoke test. The final exit code is `0` if all tests pass, non-zero otherwise.

`BASE_URL` defaults to `http://localhost:8888` but can be overridden via `sys.argv[1]`, enabling testing against staging or remote instances.

`TIMEOUT = 130.0` is set slightly above the default 120-second task timeout. This prevents the HTTP client from timing out before a long-running task completes normally, which would produce a false FAIL.

## Test Coverage

### Agent Card Discovery (`test_agent_card()`)
Fetches `/.well-known/agent.json` and validates the required A2A fields. This is the first thing any A2A client does; a malformed or missing agent card prevents all downstream interactions.

### Message Send (`test_message_send()`)
Sends a JSON-RPC `message/send` request and captures the returned `task_id`. The task ID is stored in the module-level `task_id` variable for use by subsequent tests — creating an implicit ordering dependency between tests.

### Task Get (`test_tasks_get()`)
Retrieves the task created by `test_message_send()` and validates its structure. This verifies that the server's task store persists tasks across requests.

### Terminal Guard (`test_terminal_guard()`)
Sends a second message to a completed task and expects a 4xx error. This tests the guard that prevents re-opening terminal (completed/failed) tasks, which would corrupt task state.

### Output Mode Rejection (`test_output_mode_rejection()`)
Sends a request with an unsupported output mode and expects a specific JSON-RPC error code. This validates the server's input validation layer.

### Task Cancellation (`test_cancel()`)
Creates a long-running task and immediately sends a `tasks/cancel` request, then polls until the task reaches the `cancelled` state. This tests the cancellation signal path through the agent loop.

### Streaming (`test_streaming()`)
Opens a streaming connection and validates that the server sends SSE (Server-Sent Events) chunks. Streaming tests are the most fragile because they depend on timing — the `TIMEOUT` constant exists specifically for this test.

### JSON-RPC Error Codes (`test_jsonrpc_errors()`)
Sends malformed requests and validates that the server returns the correct JSON-RPC error codes (`-32600` invalid request, `-32601` method not found, etc.).

### REST Endpoints (`test_rest_endpoints()`)
Validates that the REST-style endpoints (non-JSON-RPC) return expected status codes.

### Card Caching (`test_card_caching()`)
Fetches the agent card twice and verifies that the `ETag` or `Last-Modified` headers are present, enabling client-side caching.

## Running

```bash
# Start the server:
POCKETPAW_A2A_ENABLED=true uv run pocketpaw

# Run the smoke test:
uv run python scripts/a2a_smoke_test.py
# Or against a custom URL:
uv run python scripts/a2a_smoke_test.py http://localhost:9000
```

## Known Gaps

- Tests have an implicit ordering dependency via the shared `task_id` module variable. If `test_message_send()` fails, downstream tests will fail with confusing errors rather than being skipped.
- There is no cleanup step — tasks and sessions created during the test run persist in the database, which can interfere with subsequent runs.
- The streaming test does not validate chunk content, only that chunks are received — a server that streams garbage would pass.