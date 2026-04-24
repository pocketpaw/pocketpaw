---
{
  "title": "OpenCode Backend — REST API Client for a Running OpenCode Server",
  "summary": "Implements `OpenCodeBackend`, which communicates with a locally running OpenCode server (`opencode --server`) via its REST API using `httpx`. Sessions are mapped from PocketPaw session keys to OpenCode session IDs, and streaming responses are parsed from NDJSON parts.",
  "concepts": [
    "OpenCodeBackend",
    "OpenCode server",
    "REST API",
    "httpx",
    "session mapping",
    "NDJSON parts",
    "health check",
    "fail-fast",
    "beta backend",
    "AsyncClient"
  ],
  "categories": [
    "agent-runtime",
    "rest-client",
    "opencode",
    "streaming"
  ],
  "source_docs": [
    "73b2c3b4295a23ae"
  ],
  "backlinks": null,
  "word_count": 410,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`OpenCodeBackend` is the only PocketPaw backend that connects to an external server process rather than spawning subprocesses or importing a Python SDK. It communicates with a running `opencode --server` instance over HTTP using `httpx`, making it the lightest-weight integration from PocketPaw's perspective.

## Architecture Difference

Most PocketPaw backends either import a Python SDK (Claude SDK, OpenAI Agents, Google ADK) or spawn a subprocess (Codex CLI, Copilot SDK). OpenCode takes a third approach: it is a long-running server that PocketPaw treats as a microservice. This decouples the agent's lifecycle from PocketPaw's — the OpenCode server can be started independently, upgraded without restarting PocketPaw, or run on a remote host.

## Session Mapping

`_get_or_create_session()` calls `POST /session` to create a new OpenCode session and stores the returned session ID in `_session_map` keyed by PocketPaw's `session_key`. On subsequent messages in the same conversation, the existing session ID is reused, maintaining OpenCode's conversation context. The map is in-memory; PocketPaw restart loses the session mapping even if the OpenCode server continues running.

## Health Check on First Use

`_check_health()` issues a lightweight `GET /health` before the first message in a new session. If the OpenCode server is unreachable, the backend emits an `Error` event immediately rather than attempting `POST /session` and timing out after the full HTTP timeout. This fail-fast approach gives the user an actionable error within milliseconds.

## NDJSON Part Parsing

`POST /session/{id}/message` returns a streaming NDJSON response. `run()` iterates lines and parses each as a JSON object. Recognised part types:

| Part Type | Maps To |
|-----------|---------|
| `text` | `TextChunk` |
| `tool-input` / `tool-call` | `ToolUse` |
| `tool-result` | `ToolResult` |
| `error` | `Error` |

Unknown part types are skipped, so new OpenCode versions with additional parts do not crash the backend.

## httpx AsyncClient Reuse

`_get_client()` returns a cached `httpx.AsyncClient` with a 60-second timeout. Reusing the client avoids per-request TLS handshake and connection setup overhead. `stop()` explicitly closes the client to release OS file descriptors.

## Beta Status

`BackendInfo(beta=True)` marks this backend as experimental. The OpenCode REST API is not yet stable and breaking changes are expected; this flag excludes the backend from the default list.

## Known Gaps

- No authentication on the OpenCode endpoint — any process with network access to `opencode_base_url` can submit tasks.
- Session map is in-memory; restart breaks continuity even when the OpenCode server survives.
- No built-in tools registered in PocketPaw; tool execution occurs entirely inside OpenCode.
