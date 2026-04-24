---
{
  "title": "Agent Backends and Router Tests: ClaudeSDKBackend, Registry Routing, Dangerous Hook, and CLI Auth",
  "summary": "Tests for the multi-SDK agent architecture covering `AgentEvent` protocol, `ClaudeSDKBackend` (formerly ClaudeAgentSDKWrapper), registry-based `AgentRouter` backend selection with legacy fallbacks, the fail-closed dangerous command hook, and a regression test for PocketPaw incorrectly requiring an API key when Claude CLI authentication is active.",
  "concepts": [
    "ClaudeSDKBackend",
    "AgentRouter",
    "registry-based routing",
    "dangerous hook",
    "fail-closed",
    "backward compatibility",
    "CLI auth",
    "AgentEvent",
    "DANGEROUS_SUBSTRINGS",
    "multi-SDK architecture"
  ],
  "categories": [
    "testing",
    "agent backends",
    "security",
    "core runtime",
    "test"
  ],
  "source_docs": [
    "7bc38d1a0b4b0545"
  ],
  "backlinks": null,
  "word_count": 454,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_agents.py` covers the agent backend layer that was refactored to a registry-based multi-SDK architecture. The refactor renamed `ClaudeAgentSDKWrapper` to `ClaudeSDKBackend`, introduced backward-compatibility aliases, and replaced hard-coded backend selection with a registry lookup. The test file was updated to match, removing tests for the removed `ExecutorProtocol`, `OrchestratorProtocol`, `pocketpaw_native`, and `open_interpreter` backends.

## Test Class Breakdown

### TestAgentProtocol
Verifies `AgentEvent` construction, default `metadata={}`, and that all documented event types (`message`, `tool_use`, `tool_result`, `thinking`, `error`, `done`) are accepted. These are the wire types between the agent backend and the `AgentLoop`.

### TestClaudeAgentSDK

**Backward compatibility**: `ClaudeAgentSDK` and `ClaudeAgentSDKWrapper` must both be aliases to `ClaudeSDKBackend`. Any import that already uses the old names must continue to work after the rename.

**Info static method**: `ClaudeSDKBackend.info()` returns an `AgentBackendInfo` describing the backend's display name, built-in tools, and tool policy map. This is used by the router to display backend capabilities in the settings UI.

**Dangerous pattern detection**: `_is_dangerous_command` matches against `DANGEROUS_SUBSTRINGS`. Tests confirm `rm -rf /`, `rm -rf ~`, and `sudo rm /important` are detected, while `ls -la` and `cat file.txt` are not.

**Fail-closed hook** — GH-852 regression guard:

```python
async def test_dangerous_hook_fails_closed_on_exception():
    with patch.object(sdk, "_is_dangerous_command", side_effect=RuntimeError("boom")):
        result = await sdk._block_dangerous_hook(input_data, None, None)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
```

Before the fix, an internal error in the hook returned `{}` (implicitly allowing the command). The fix catches all exceptions and returns a deny decision with an `"internal error"` reason. This is safety-critical: a hook that allows commands on error is worse than one that fails closed.

### TestAgentRouter

**Registry-based selection**: The router defaults to `claude_agent_sdk`. Legacy backend names (`pocketpaw_native`, `open_interpreter`) fall back to the default rather than raising.

**Fallback on unknown**: An unrecognised backend name falls back to the default backend, preventing a misconfigured settings value from breaking the entire agent pipeline.

**Run and stop methods**: The router exposes `run()` (an async generator) and `stop()` (an async method). Both are tested to be present and callable — these are the interface contracts the `AgentLoop` depends on.

### TestClaudeSDKCliAuth — API Key Bug Regression

This class reproduces a specific bug: PocketPaw would require an Anthropic API key even when the user was authenticated via Claude CLI (which manages its own auth). Tests verify:
- Without an API key, `auto_resolve` selects Ollama (not Anthropic).
- With `force_anthropic=True` and no key, the `anthropic` backend is returned but without a key (the API layer handles the error gracefully).
- When `ClaudeSDKBackend.run()` is invoked, it resolves to `anthropic` (using CLI auth) rather than falling through to Ollama.

## Known Gaps

No test covers the `AgentRouter.get_backend_info()` path when the configured backend raises during import (a broken plugin scenario). The dangerous pattern list in `DANGEROUS_SUBSTRINGS` is tested indirectly but the full list is not enumerated in tests.