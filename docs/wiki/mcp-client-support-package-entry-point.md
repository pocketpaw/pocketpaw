---
{
  "title": "MCP Client Support Package Entry Point",
  "summary": "The `pocketpaw.mcp` package provides PocketPaw with the ability to connect to any Model Context Protocol (MCP) server and invoke its tools without requiring custom tool implementations for each integration. This module re-exports all public symbols from the config and manager sub-modules, giving consumers a single clean import surface.",
  "concepts": [
    "MCP",
    "Model Context Protocol",
    "MCPManager",
    "MCPServerConfig",
    "MCPToolInfo",
    "tool discovery",
    "package exports",
    "singleton",
    "agent integration"
  ],
  "categories": [
    "MCP Integration",
    "Package Architecture"
  ],
  "source_docs": [
    "429b6897951b0e94"
  ],
  "backlinks": null,
  "word_count": 428,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `pocketpaw.mcp` package implements PocketPaw's client-side support for the Model Context Protocol. MCP is an open protocol that lets AI agents discover and call tools exposed by external servers over a standardised interface. By adopting MCP, PocketPaw avoids writing bespoke tool wrappers for every integration — any MCP-compliant server (GitHub, Notion, custom internal tooling) becomes usable immediately.

## Package Structure

This `__init__.py` is a pure re-export module. It exists to give callers a flat, stable import surface:

```python
from pocketpaw.mcp import MCPManager, MCPServerConfig, MCPToolInfo
from pocketpaw.mcp import get_mcp_manager, load_mcp_config, save_mcp_config
```

Without this file, consumers would need to know whether to import from `pocketpaw.mcp.manager` or `pocketpaw.mcp.config`. Centralising exports here means the internal sub-module layout can be refactored without breaking downstream imports.

## Exported Symbols

| Symbol | Origin | Purpose |
|--------|--------|---------|
| `MCPManager` | `mcp.manager` | Singleton managing server lifecycles and tool calls |
| `MCPServerConfig` | `mcp.config` | Dataclass for a single server's configuration |
| `MCPToolInfo` | `mcp.manager` | Metadata about a tool discovered from an MCP server |
| `get_mcp_manager()` | `mcp.manager` | Factory for the `MCPManager` singleton |
| `load_mcp_config()` | `mcp.config` | Reads `~/.pocketpaw/mcp_servers.json` |
| `save_mcp_config()` | `mcp.config` | Persists server configs to disk |

## Design Rationale

Separating concerns into `config.py` and `manager.py` keeps the package maintainable. Config handles pure data persistence; the manager handles async I/O, subprocess lifecycle, and OAuth. The `__init__.py` stitches them together so the rest of the codebase sees a single `pocketpaw.mcp` namespace.

## Why MCP Matters for PocketPaw

Before MCP, adding a new integration to PocketPaw meant writing a custom `ToolProtocol` implementation, handling auth, building result serialisation, and wiring it into the agent router. Each integration was one-off work. MCP turns this into a standard: any server that speaks the protocol is immediately usable by any PocketPaw agent backend without any PocketPaw-specific code.

This is particularly important as PocketPaw grows. The MCP ecosystem includes hundreds of third-party servers — for GitHub, Notion, Slack, databases, web search, and more. By implementing MCP client support once, PocketPaw gains access to all of them simultaneously.

## Usage Pattern

At application startup, the main app calls `get_mcp_manager().start_enabled_servers()`. This reads `~/.pocketpaw/mcp_servers.json` via `load_mcp_config`, starts each enabled server, and caches the discovered tools. Agent backends then call `get_mcp_manager().get_all_tools()` to enumerate available tools and `call_tool(server, tool, args)` to invoke them. The agent code never touches config or transport details.

## Known Gaps

None flagged in this file. The real complexity — and potential gaps — lives in `manager.py` (connection handling, OAuth flows, error recovery) and `config.py` (file locking, plaintext secrets).