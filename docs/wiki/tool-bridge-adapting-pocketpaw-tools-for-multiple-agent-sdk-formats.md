---
{
  "title": "Tool Bridge: Adapting PocketPaw Tools for Multiple Agent SDK Formats",
  "summary": "The tool bridge discovers all registered PocketPaw tools and wraps them in the native function-tool format expected by each agent backend (OpenAI Agents SDK `FunctionTool`, Google ADK `FunctionTool`, LangChain `StructuredTool`). It enforces backend-aware exclusion rules — the Claude Agent SDK gets shell/FS/edit tools excluded because the CLI provides them natively — and scans tool outputs for injection attacks before returning results to the agent.",
  "concepts": [
    "tool bridge",
    "FunctionTool",
    "StructuredTool",
    "backend exclusion",
    "injection scanning",
    "OpenAI Agents SDK",
    "Google ADK",
    "LangChain",
    "closure capture bug",
    "system prompt injection"
  ],
  "categories": [
    "agents",
    "tools",
    "security",
    "multi-backend"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 524,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's tool ecosystem (search, memory, browser, shell, file edit, etc.) is defined once using its own `ToolProtocol`. Each agent SDK, however, expects tools in a different format. The tool bridge is the translation layer that lets PocketPaw tools work across all supported backends without duplicating tool definitions.

## Backend-Aware Tool Discovery

`_instantiate_all_tools(backend)` scans the tool registry and instantiates all built-in tools, then filters by backend. The exclusion list for `claude_agent_sdk` is explicit and important:

- Shell tools (`ShellTool`) — Claude Code already provides a native bash/shell capability
- File system tools — Claude Code has native Read/Write/Edit
- `EditFileTool` — added to the exclusion list on 2026-03-12 when Claude Code shipped native Edit

This exclusion prevents the agent from seeing two competing ways to edit a file (the PocketPaw wrapper and the native Claude Code tool), which would create confusing duplicate tool listings and potential conflicts in tool choice. All other backends receive the full tool set including shell/FS/edit, since they have no native equivalent.

`BrowserTool` and `DesktopTool` are always excluded across all backends because they require special session state (a browser process, an OS-level accessibility session) that is not available in headless agent contexts.

## SDK-Specific Wrappers

### OpenAI Agents SDK

`build_openai_function_tools` wraps each PocketPaw tool as an OpenAI `FunctionTool` object. The async invoke callback is created by `_make_invoke_callback(tool)`, which uses a factory function rather than a direct closure to avoid the classic Python closure variable capture bug — if you write `lambda: tool.invoke(...)` inside a loop, all lambdas capture the same final `tool` reference.

### Google ADK

`build_adk_function_tools` uses `_make_adk_wrapper(tool)`, which returns an async function typed with the tool's JSON schema parameters. ADK requires the wrapper function's signature to match the JSON schema exactly; the bridge dynamically constructs a compatible function signature from the tool's Pydantic input model.

### LangChain / Deep Agents

`build_deep_agents_tools` wraps tools as LangChain `StructuredTool` objects via `_make_langchain_wrapper`. LangChain's structured tool format aligns most closely with PocketPaw's own schema, making this the simplest of the three wrappers.

## Injection Attack Scanning

`_scan_tool_output(result, tool_name)` runs on every tool result before it is returned to the agent. It looks for patterns that indicate prompt injection attempts embedded in tool output — for example, a web page that contains "Ignore all previous instructions." If a suspicious pattern is detected, the result is replaced with a sanitized placeholder and a warning is logged.

This defense is important because external data sources (search results, web pages, files) are attacker-controlled. Without output scanning, a malicious document could redirect the agent to exfiltrate data or take unintended actions.

## System Prompt Injection

`get_tool_instructions_compact` produces a compressed markdown block describing all available tools, suitable for injection into the agent's system prompt. This is used by backends that do not support native function-calling schemas and instead rely on the agent reading tool documentation from the prompt.

## Known Gaps

- The injection scan patterns are not documented or externally configurable. Adding new attack patterns requires a code change.
- ADK wrapper signature construction from Pydantic models handles simple scalar fields well but may not correctly represent nested objects or `Optional` union types in all cases.
