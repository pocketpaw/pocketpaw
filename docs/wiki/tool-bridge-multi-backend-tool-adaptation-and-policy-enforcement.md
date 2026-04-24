---
{
  "title": "Tool Bridge: Multi-Backend Tool Adaptation and Policy Enforcement",
  "summary": "The tool bridge (`agents/tool_bridge.py`) adapts PocketPaw's internal tool registry for multiple AI backend SDKs — OpenAI Agents, Google ADK, Claude Agent SDK, and others — while enforcing tool policies and generating compact markdown instructions. Tests cover backend-specific tool exclusions, graceful import failure handling, OpenAI schema normalization, invoke callback error handling, and policy-filtered instruction generation.",
  "concepts": [
    "tool_bridge",
    "multi-backend",
    "tool_exclusions",
    "policy_filtering",
    "FunctionTool",
    "invoke_callback",
    "schema_normalization",
    "graceful_degradation",
    "claude_agent_sdk",
    "openai_agents",
    "tool_instructions",
    "BaseTool"
  ],
  "categories": [
    "tool-system",
    "agent-backends",
    "testing",
    "test"
  ],
  "source_docs": [
    "ff3162b166416a9c"
  ],
  "backlinks": null,
  "word_count": 624,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The tool bridge is PocketPaw's translation layer between its internal `BaseTool` protocol and the diverse function-calling schemas expected by different AI agent backends. Because each backend (OpenAI Agents SDK, Claude Agent SDK, Google ADK, Codex CLI, Copilot SDK) has its own tool registration format and capability expectations, the bridge must adapt the same set of tools differently depending on the runtime target.

## Backend-Specific Tool Exclusions

Not all tools are safe or meaningful across all backends. The bridge enforces two categories of exclusions:

**Claude Agent SDK exclusions** (`_CLAUDE_SDK_EXCLUDED`): When running under `claude_agent_sdk`, `ShellTool`, `ReadFileTool`, `WriteFileTool`, and `ListDirTool` are stripped. This is because the Claude SDK manages file and shell access through its own internal computer-use pipeline — providing a duplicate shell tool would create conflicting execution paths and potential security double-execution.

**Universal exclusions**: `BrowserTool` and `DesktopTool` are excluded for every backend. These require a live desktop/browser environment and would crash headless agent deployments. The tests verify this invariant across all backends to prevent accidental capability leakage.

```python
def test_excludes_browser_and_desktop_always(self):
    for backend in ["claude_agent_sdk", "openai_agents", "google_adk"]:
        tools = _instantiate_all_tools(backend=backend)
        names = {type(t).__name__ for t in tools}
        assert "BrowserTool" not in names
        assert "DesktopTool" not in names
```

## Graceful Import Failure Handling

Tool modules are loaded dynamically via `importlib`. If any module fails to import (e.g., a missing optional dependency like `pyautogui` for the screenshot tool), the bridge catches the `ImportError` and skips that tool rather than aborting startup. Without this guard, a single bad optional dependency would render the entire agent non-functional. The test validates this by patching `importlib.import_module` to universally raise `ImportError`, expecting an empty but valid list in return.

## OpenAI Schema Normalization

OpenAI's strict mode requires that zero-argument tools carry an explicit `{"type": "object", "properties": {}}` schema — it will reject a missing or null schema. The bridge preserves this schema when building `FunctionTool` wrappers, preventing silent schema validation failures at the OpenAI API boundary. The test checks the actual `kwargs` passed to `FunctionTool.__init__` to confirm the schema is forwarded intact.

## Policy Filtering in Tool Wrappers

When building the tool list for any backend, `build_openai_function_tools` consults the active `ToolPolicy` (from `Settings`) to exclude denied tools and apply profile restrictions. This means that if `tools_deny=["web_search"]` is set, the tool simply never appears in the SDK's registered tool list — there is no runtime check to bypass. The `minimal` profile test confirms that only memory and session tools (`remember`, `recall`) pass through when a restrictive profile is active.

## Invoke Callback Error Handling

`_make_invoke_callback` wraps each tool's async `execute` method into the callback shape expected by the OpenAI Agents SDK. Four failure modes are explicitly tested:

- **Invalid JSON input**: Returns an error string rather than crashing, preventing agent loops from hanging on malformed tool arguments.
- **Empty argument string**: Treated as a zero-argument call (`execute()`) rather than a parse error, supporting tools like `recall` that take no parameters.
- **Execution exceptions**: Any `RuntimeError` or other exception from the tool's `execute` method is caught and returned as an error string. This keeps the agent's event loop alive even if a tool's API dependency is down.
- **Valid JSON**: Arguments are unpacked as kwargs and forwarded to `execute`.

## Compact Tool Instructions

`get_tool_instructions_compact` generates a concise markdown reference of available tools for injection into system prompts. It respects the same policy filtering, so denied tools never appear in the instructions either — preventing the agent from being instructed to use tools it cannot actually call.

## Known Gaps

None flagged in the source. The test for `build_openai_function_tools` requires reimporting the module inside the `sys.modules` patch context, suggesting the FunctionTool import is done at call time (lazy import) rather than at module load, which is a deliberate workaround for optional SDK availability.