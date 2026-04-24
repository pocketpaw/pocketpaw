---
{
  "title": "Deep Agents Tool Bridge: LangChain Integration and Graceful Degradation",
  "summary": "These tests cover the `build_deep_agents_tools` bridge function which adapts PocketPaw tools for LangChain-based Deep Agents backends. The key behavior tested is that the function returns an empty list gracefully when `langchain-core` is not installed, and that the Deep Agents backend is not subject to the same tool exclusions as the Claude Agent SDK.",
  "concepts": [
    "deep_agents",
    "langchain",
    "build_deep_agents_tools",
    "graceful_degradation",
    "_CLAUDE_SDK_EXCLUDED",
    "optional_dependency",
    "tool_bridge",
    "LangChain"
  ],
  "categories": [
    "tool-system",
    "agent-backends",
    "testing",
    "test"
  ],
  "source_docs": [
    "4cc288af00e21415"
  ],
  "backlinks": null,
  "word_count": 329,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports a `deep_agents` backend that integrates with LangChain's tool protocol via `langchain-core`. Because LangChain is a heavyweight optional dependency — not required for basic PocketPaw operation — the bridge must degrade gracefully when it is absent rather than crashing the runtime.

## Graceful Degradation Without LangChain

The primary test verifies that `build_deep_agents_tools` returns an empty list `[]` when `langchain_core` and `langchain_core.tools` are patched out of `sys.modules`. This prevents an `ImportError` from propagating to the agent router and crashing the session.

```python
def test_build_deep_agents_tools_graceful_degradation(self):
    from pocketpaw.agents.tool_bridge import build_deep_agents_tools
    with patch.dict("sys.modules", {"langchain_core": None, "langchain_core.tools": None}):
        result = build_deep_agents_tools(Settings(), backend="deep_agents")
        assert result == []
```

The failure this prevents: without this guard, any environment that installs PocketPaw without LangChain would crash the moment an agent tried to use the Deep Agents backend, making the backend effectively unusable as an optional feature.

## Separation from Claude SDK Exclusions

The second test ensures that `deep_agents` is not treated as a Claude-family backend. The `_CLAUDE_SDK_EXCLUDED` set (which strips `ShellTool`, `ReadFileTool`, etc.) only applies to `claude_agent_sdk`. Since `"deep_agents" != "claude_agent_sdk"`, all tool categories including shell and filesystem are available to the Deep Agents backend.

This distinction matters because Deep Agents typically runs in server-side Python environments where shell and file access are legitimate and expected — suppressing them would break workflows that rely on those capabilities under LangChain.

## Architecture Context

The Deep Agents bridge sits alongside `build_openai_function_tools` and backend-specific wrappers in `agents/tool_bridge.py`. Each function is responsible for converting PocketPaw's `BaseTool` instances into the format expected by a particular framework. The Deep Agents adapter converts tools into LangChain `StructuredTool` or compatible objects, enabling use in LangGraph and other LangChain-based pipelines.

## Known Gaps

The test suite for this module is intentionally minimal (two tests). Full LangChain integration tests would require `langchain-core` to be installed in the test environment, which may not always be the case. More comprehensive tests of the actual tool wrapping logic (schema conversion, async execution bridging) are not yet present.