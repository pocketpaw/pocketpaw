---
{
  "title": "MCP Server Configuration — Persistence and Schema",
  "summary": "`pocketpaw.mcp.config` defines the `MCPServerConfig` dataclass and the load/save helpers that persist MCP server definitions to `~/.pocketpaw/mcp_servers.json`. It is the single source of truth for which MCP servers are configured and how to connect to them.",
  "concepts": [
    "MCPServerConfig",
    "mcp_servers.json",
    "config persistence",
    "stdio transport",
    "HTTP transport",
    "OAuth",
    "backward compatibility",
    "registry_ref",
    "load_mcp_config",
    "save_mcp_config"
  ],
  "categories": [
    "MCP Integration",
    "Configuration Management"
  ],
  "source_docs": [
    "288d634a6446ac01"
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

Before PocketPaw can use any MCP server, it needs to know how to reach it: transport type, command, URL, credentials, timeout, and whether OAuth is required. `config.py` owns that knowledge. It defines the schema (`MCPServerConfig`) and the I/O functions (`load_mcp_config`, `save_mcp_config`) that read and write the on-disk registry at `~/.pocketpaw/mcp_servers.json`.

## MCPServerConfig Fields

```python
@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"        # "stdio" | "http" | "streamable-http"
    command: str = ""               # stdio: executable path
    args: list[str] = field(...)    # stdio: CLI arguments
    url: str = ""                   # http/streamable-http: server URL
    env: dict[str, str] = field(...)
    enabled: bool = True
    timeout: int = 30               # connection timeout in seconds
    registry_ref: str = ""          # legacy, backward compat only
    oauth: bool = False
```

The `transport` field drives which connection strategy `MCPManager` uses. Having it live on the config rather than being inferred makes the connection logic deterministic and testable without network access.

The `registry_ref` field is kept for backward compatibility with servers that were installed from a now-removed MCP Registry tab. New servers never set it; existing configs must not lose it on save.

## Load / Save Pattern

`load_mcp_config()` reads the JSON file and deserialises each entry via `MCPServerConfig.from_dict()`. If the file is missing or malformed, it logs a warning and returns an empty list rather than crashing. This prevents the entire PocketPaw startup from failing because of a corrupt config file.

`save_mcp_config()` serialises each config via `to_dict()` and writes the file. The `to_dict` method intentionally omits `registry_ref` and `oauth` when they are falsy, keeping the JSON compact for the common case.

## Why `~/.pocketpaw/mcp_servers.json`?

Storing config in the user's home directory via `get_config_dir()` means configurations survive PocketPaw upgrades and are shared across all agent backends running on the same machine. MCP server credentials (API keys in `env`) are sensitive and should live under controlled file-system permissions, not in version control.

## Defensive Patterns

`from_dict` uses `.get()` with defaults so configs written by older PocketPaw versions (lacking `timeout`, `oauth`, or `registry_ref`) load without errors. `_get_mcp_config_path()` is private; callers always go through the load/save functions, preventing path construction bugs from spreading.

## Enabled Flag

The `enabled: bool = True` field means servers can be disabled without deleting their configuration. This is valuable for temporarily pausing an integration (e.g., an MCP server whose API key has expired) without losing its configuration. At startup, `MCPManager.start_enabled_servers()` filters on this flag and skips disabled servers. Re-enabling is a single config update.

## Timeout Field

The `timeout: int = 30` field sets the connection timeout per server. Different servers have different startup latency — a local stdio server spawns in milliseconds, while a remote SSE server over a slow network may need 15-20 seconds. Without per-server timeouts, a single slow server would block all subsequent servers from starting, or a single global timeout would be too aggressive for slow ones and too lenient for fast ones.

## Known Gaps

No file-locking on write: if two processes save simultaneously, the last write wins and the other's changes are silently discarded. The `env` dict is stored in plain text; secrets (API keys) are not encrypted at rest.