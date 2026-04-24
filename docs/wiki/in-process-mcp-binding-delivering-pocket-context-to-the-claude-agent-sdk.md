---
{
  "title": "In-Process MCP Binding: Delivering Pocket Context to the Claude Agent SDK",
  "summary": "This module registers an in-process MCP server named `pocketpaw_pocket` that exposes a `get_pocket` tool, working around a Windows `CreateProcess` command-line length limit of ~32KB that would be exceeded if pocket documents were embedded directly in the system prompt. The actual pocket data is fetched lazily from `ee/cloud/pockets/agent_context.py` only when the agent calls the tool.",
  "concepts": [
    "MCP in-process server",
    "WinError 206",
    "CreateProcess limit",
    "get_pocket tool",
    "pocket context",
    "rippleSpec",
    "tool allowlist",
    "enterprise module",
    "SDK adapter",
    "command-line length limit"
  ],
  "categories": [
    "agents",
    "MCP",
    "Windows compatibility",
    "enterprise"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 476,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## The Problem: WinError 206

The Claude Agent SDK launches `claude.exe` as a subprocess and passes the system prompt as a command-line argument. Windows `CreateProcess` caps command lines at approximately 32,767 characters (WinError 206). A full pocket document — especially one containing a `rippleSpec.ui` widget tree with many nested nodes — can easily exceed this limit.

Embedding pocket context directly in the system prompt works on macOS/Linux where argument length limits are much higher (and the SDK uses `execve` rather than `CreateProcess`), but the same binary must run on Windows. Rather than maintaining a platform-conditional code path in the system prompt assembly, PocketPaw offloads the delivery mechanism entirely.

## The Solution: In-Process MCP Tool

`sdk_mcp_pocket.py` registers a Model Context Protocol server (`pocketpaw_pocket`) inside the same process as the agent runtime. The SDK discovers this server automatically and adds `mcp__pocketpaw_pocket__get_pocket` to the agent's available tool set. When the agent needs pocket context, it calls the tool; the response travels through the SDK's stdio JSON channel (unbounded in size) and never touches the command line.

The tool ID constant:

```python
GET_POCKET_TOOL_ID = f"mcp__{SERVER_NAME}__get_pocket"
```

matches the exact namespace format Claude Code uses for in-process MCP tools (`mcp__<server>__<tool>`). This string must appear verbatim in the agent's tool allowlist — the comment in the source makes this explicit so maintainers do not accidentally rename the constant.

## Handler Design

`_get_pocket_handler(args)` is intentionally thin. It:

1. Imports `fetch_pocket_for_agent` lazily from the enterprise module (`ee/cloud/pockets/agent_context.py`)
2. Awaits the fetch with the `pocket_id` from `args`
3. Returns a standard MCP content block (`{"content": [{"type": "text", "text": ...}]}`)
4. On error, returns an error content block rather than raising — MCP tool errors should be communicated as tool results, not exceptions, so the agent can reason about the failure

The lazy import of the `ee` module is deliberate: the open-source core of PocketPaw can import this module without failing even when the enterprise layer is absent (the handler would simply error at call time).

## Adapter Pattern

The module docstring describes itself as "a thin adapter." The separation between `sdk_mcp_pocket.py` (MCP registration and message format) and `agent_context.py` (actual data retrieval logic) keeps concerns clean. Changing the pocket data source does not require touching MCP registration code, and changing the MCP server name does not require touching data fetching.

## Known Gaps

- The module does not define how the MCP server is actually registered with the SDK — that wiring presumably happens in the Claude SDK backend (`claude_sdk.py`). If the registration call is missing or conditional, the tool silently does not exist.
- There is no fallback for non-Windows platforms that would benefit from always using MCP delivery for large pockets (e.g., pockets with very large `rippleSpec` trees could still overflow Linux `ARG_MAX` in extreme cases).
- The `ee/` import makes this module silently non-functional in open-source deployments without clear documentation of the dependency.
