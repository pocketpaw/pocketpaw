---
{
  "title": "A2A Client Tests: HTTP Operations, Context Manager Reuse, Streaming, Delegate Tool, and SSRF Protection",
  "summary": "Tests for the PocketPaw Agent-to-Agent (A2A) client library, covering HTTP task operations (send, get, cancel, stream), response error handling, agent card fetching with caching, the `A2ADelegateTool` multi-turn wrapper, and SSRF protection that blocks requests to private IP ranges and invalid schemes.",
  "concepts": [
    "A2A client",
    "Agent-to-Agent",
    "SSRF protection",
    "agent card",
    "httpx",
    "context manager",
    "multi-turn delegation",
    "A2ADelegateTool",
    "streaming SSE",
    "private IP blocking"
  ],
  "categories": [
    "testing",
    "agent communication",
    "security",
    "A2A protocol",
    "test"
  ],
  "source_docs": [
    "47cf7e0df75502f9"
  ],
  "backlinks": null,
  "word_count": 540,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_a2a_client.py` tests the A2A client that enables PocketPaw agents to delegate tasks to remote A2A-compatible agents. The A2A protocol (Agent-to-Agent) defines a JSON-RPC 2.0 interface for task dispatching, status polling, and streaming. The client wraps `httpx.AsyncClient` and adds agent-card discovery, response normalisation, and security guards.

## Why Each Test Class Exists

### TestA2AClient — Core HTTP Operations

**Error handling helpers**: `_handle_response` and `_check_status` translate `httpx.HTTPStatusError` into `RuntimeError` with a human-readable message that includes the status code and response body. Without this translation, callers would receive raw httpx exceptions with no indication of which remote agent failed or why.

**Agent card caching**: `get_agent_card` fetches `/.well-known/agent.json` from the remote agent and caches the result. The `test_agent_card_cached_on_second_call` test verifies the cache — a second call must not issue a second HTTP request. Without caching, every tool invocation would add a round-trip, and agents with slow card endpoints would bottleneck the entire delegation chain.

**Context manager reuse**: `A2AClient` as a context manager shares a single `httpx.AsyncClient` across all calls within the block. `test_context_manager_reuses_client` asserts this by checking that only one client instance was created. Reuse matters because `httpx.AsyncClient` maintains a connection pool; creating a new instance per call would lose the pool and degrade throughput.

**Auth header propagation**: `test_auth_headers_passed_to_requests` verifies that bearer tokens or API keys passed at construction time appear in every outgoing request header.

**Message response handling**: Some A2A agents respond to `tasks/send` with a message (not a task) when they process the request synchronously. `test_send_task_handles_message_response` covers that branch.

### TestA2ADelegateTool — Tool Wrapper

`A2ADelegateTool` is the PocketPaw tool that agents call to delegate to a remote A2A peer. Tests cover:
- **Happy path**: card fetch + task send + artifact extraction.
- **Card fetch failure**: `RuntimeError` propagates cleanly to the calling agent as a tool error.
- **Multi-turn support**: When the remote agent returns `input-required`, the tool enters a multi-turn loop, re-sending the conversation history until the task completes or the agent signals it does not support multi-turn.
- **Artifacts**: The tool includes artifact content in the result when the remote task produces files or structured data. When there are no artifacts, the `artifacts` key is omitted entirely (not set to `null` or `[]`) to keep the tool result compact.

### TestSSRFProtection — Security

SSRF (Server-Side Request Forgery) is a class of attack where a user-controlled URL causes the server to make requests to internal infrastructure. The tests verify three guards:

```python
async def test_ssrf_private_ip_blocked():
    # 192.168.x.x, 10.x.x.x, 127.x.x.x etc. must be blocked.
    ...
async def test_ssrf_invalid_scheme_blocked():
    # Only http/https are allowed — file://, ftp:// etc. must raise.
    ...
async def test_ssrf_dns_resolution_failure():
    # A hostname that cannot be resolved must raise cleanly.
    ...
async def test_ssrf_public_ip_allowed(mock_agent_card, mock_task):
    # A legitimate public IP must pass through to the real request path.
    ...
```

The private-IP block prevents a compromised agent card URL (e.g., pointing at `http://169.254.169.254/metadata`) from leaking cloud instance credentials. The scheme check prevents the `file://` URI trick that would let an attacker read local files via the A2A delegation path.

## Known Gaps

No test covers the streaming partial-chunk case where the SSE stream is interrupted mid-response. The multi-turn loop has no maximum iteration guard tested — an adversarial remote agent that always returns `input-required` would loop indefinitely.