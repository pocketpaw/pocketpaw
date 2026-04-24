---
{
  "title": "ClaudeAgentSDK Dispatch and Persistent Client Tests",
  "summary": "This module tests the dispatch logic for `ClaudeAgentSDK`, documenting the removal of the `_fast_chat` direct-API bypass in version 0.4.16 and the introduction of a persistent `ClaudeSDKClient` that reuses connections across calls. It verifies that all task complexities (SIMPLE, MODERATE, routing disabled) flow through the CLI path and validates the client lifecycle: creation, reuse, reconnection on model change, fallback on failure, interrupt, and cleanup.",
  "concepts": [
    "ClaudeAgentSDK",
    "fast_chat removal",
    "persistent client",
    "ClaudeSDKClient",
    "conversation compaction",
    "model routing",
    "TaskComplexity",
    "smart_routing_enabled",
    "fallback",
    "interrupt",
    "cleanup",
    "context overflow"
  ],
  "categories": [
    "testing",
    "agent backend",
    "Claude SDK integration",
    "test"
  ],
  "source_docs": [
    "db3c2a4986b768ec"
  ],
  "backlinks": null,
  "word_count": 424,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Background: Why `_fast_chat` Was Removed

Before 0.4.16, `ClaudeAgentSDK` had a shortcut called `_fast_chat` that bypassed the Claude Code CLI and called the Anthropic API directly for low-complexity (SIMPLE) messages. This seemed efficient, but it sidestepped the CLI's built-in conversation compaction mechanism. On long sessions the context window would overflow with no recovery path, causing unrecoverable errors. The fix unified all traffic through the persistent CLI path, which handles compaction automatically.

This test file exists to document that decision and lock in the new behavior so no future refactor accidentally reintroduces the bypass.

## Test Infrastructure

Because `ClaudeAgentSDK` depends on the Claude SDK being importable (which it may not be in CI), the helpers work around this:

```python
def _make_sdk(settings=None):
    with patch("pocketpaw.agents.claude_sdk.ClaudeAgentSDK._initialize"):
        sdk = ClaudeAgentSDK(s)
    sdk._sdk_available = True
    sdk._cli_available = True
    sdk._HookMatcher = lambda matcher, hooks: MagicMock()
    sdk._ClaudeAgentOptions = lambda **kw: MagicMock()
    return sdk
```

Three fake classes simulate the SDK's streaming interface: `_FakeTextStream` (async iterator), `_FakeStreamCM` (async context manager wrapping the stream), and `_FakeSDKClient` (a full mock of `ClaudeSDKClient` tracking `connected`, `queries`, `interrupted`, and `disconnected` state).

## Dispatch Tests

**SIMPLE via CLI path** (`test_chat_dispatches_fast_path_for_simple`): Sends a SIMPLE-classified message and asserts it lands in `fake_client.queries`, proving the persistent client handles it — not a direct API call.

**MODERATE via CLI path** (`test_chat_uses_persistent_client_for_moderate`): Same pattern for MODERATE complexity, establishing the baseline.

**Routing disabled** (`test_chat_standard_path_when_routing_disabled`): With `smart_routing_enabled=False`, the router is skipped and messages still go through the persistent client. A `ResultMessage` response is used to exercise the clean-shutdown path.

## Persistent Client Lifecycle

**Reuse** (`test_persistent_client_reuse`): Two consecutive calls with identical model and tool options must produce exactly one client instance. The test counts `clients_created` to assert this.

**Reconnect on model change** (`test_persistent_client_reconnects_on_model_change`): Switching from `claude-sonnet` to `claude-haiku` must disconnect the old client and create a new one. The test verifies `client1.disconnected` is `True` after the switch.

**Fallback on failure** (`test_persistent_client_falls_back_to_query`): If `ClaudeSDKClient` instantiation raises `RuntimeError`, `chat()` falls back to the stateless `_query()` method. The test installs a `_broken_factory` and a `_fake_query` replacement, then asserts `fallback_called` is `True`.

**Stop/interrupt** (`test_stop_interrupts_persistent_client`): Calling `stop()` sets `_stop_flag`, calls `interrupt()` on the client, then disconnects it.

**Cleanup** (`test_cleanup_disconnects_client`): `cleanup()` disconnects the client and sets `_client` and `_client_options_key` back to `None`.

**Cleanup idempotency** (`test_cleanup_noop_when_no_client`): `cleanup()` must not raise when no client exists, making it safe to call unconditionally during shutdown.

## Known Gaps

No `TODO` or `FIXME` markers. The fake stream classes do not simulate backpressure or partial reads, so tests that depend on chunk ordering are only approximate. There is no test for what happens when the persistent client disconnects mid-stream.