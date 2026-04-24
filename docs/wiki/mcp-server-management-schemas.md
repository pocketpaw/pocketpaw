---
{
  "title": "MCP Server Management Schemas",
  "summary": "Defines the Pydantic request models for managing Model Context Protocol (MCP) servers in PocketPaw — adding servers by configuration, referencing them by name, test-connecting before saving, and installing from a curated preset library. These schemas expose the full MCP lifecycle through the REST API.",
  "concepts": [
    "MCPServerAddRequest",
    "MCPTestRequest",
    "MCPPresetInstallRequest",
    "Model Context Protocol",
    "MCP",
    "stdio transport",
    "HTTP transport",
    "tool servers",
    "preset library",
    "Pydantic validation"
  ],
  "categories": [
    "api-schemas",
    "mcp",
    "tool-management",
    "configuration"
  ],
  "source_docs": [
    "c547c938d411bcb5"
  ],
  "backlinks": null,
  "word_count": 576,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports the Model Context Protocol (MCP), which allows agents to communicate with external tool servers over either stdio (local subprocess) or HTTP (remote URL). This file defines the request schemas for the MCP management API — the backend surface that the dashboard's MCP settings panel calls to add, test, remove, and install servers.

## Why MCP Matters

MCP extends an agent's tool surface beyond its built-in capabilities. An MCP server might expose database queries, calendar access, code execution sandboxes, or proprietary business APIs. Managing MCP servers safely requires validating configuration before writing it to disk — a malformed server entry would break the agent's startup sequence.

## Models

### `MCPServerAddRequest`

```python
class MCPServerAddRequest(BaseModel):
    name: str = Field(..., min_length=1)
    transport: str = "stdio"
    command: str = ""
    args: list[str] = []
    url: str = ""
    env: dict[str, str] = {}
    enabled: bool = True
```

This schema handles both transport types in one model:
- **stdio servers** use `command` + `args` to spawn a local subprocess.
- **HTTP servers** use `url` to connect to a remote endpoint.

Having a single schema for both avoids separate endpoints while keeping the API surface small. The backend discriminates on `transport` to know which fields are required. `env` allows passing credentials (API keys, tokens) as environment variables rather than embedding them in the command string — a security improvement since env vars don't appear in process listings.

`enabled: bool = True` means newly added servers are active by default. This is a usability choice — the user just added it and presumably wants it working immediately.

### `MCPServerNameRequest`

```python
class MCPServerNameRequest(BaseModel):
    name: str
```

A minimal reference model used for operations that only need to identify a server — such as removing it, enabling/disabling it, or fetching its tool list. Keeping this separate avoids sending a full configuration payload for simple name-keyed operations.

### `MCPTestRequest`

```python
class MCPTestRequest(BaseModel):
    name: str = "test"
    transport: str = "stdio"
    command: str = ""
    args: list[str] = []
    url: str = ""
    env: dict[str, str] = {}
```

Mirrors `MCPServerAddRequest` but without `enabled` — this is a dry-run connectivity check. The test endpoint attempts to connect to the server using the provided configuration and returns success/failure before the server is persisted. This prevents the common failure mode of saving a broken MCP server config that renders the agent unable to start.

### `MCPPresetInstallRequest`

```python
class MCPPresetInstallRequest(BaseModel):
    preset_id: str
    env: dict[str, str] = {}
    extra_args: list[str] | None = None
```

PocketPaw ships with a curated preset library — known-good MCP server configurations for popular integrations. `preset_id` references a preset by identifier (e.g. `"filesystem"`, `"github"`, `"postgres"`). The `env` dict lets users supply secrets specific to their environment (API keys, connection strings) without the preset needing to know them in advance. `extra_args` allows overriding default command arguments for advanced users.

## Defensive Patterns

- `name: str = Field(..., min_length=1)` prevents empty-string server names that would break config key lookup.
- Separating `MCPTestRequest` from `MCPServerAddRequest` enforces a test-before-save workflow, catching connection failures early.
- `env` as `dict[str, str]` (not `dict`) enforces string values, preventing non-serialisable types from entering the config file.

## Known Gaps

- No URL validation on `url` field — malformed URLs are accepted at the schema level and fail only at connection time.
- `transport` is an unconstrained string; there's no `Literal["stdio", "http"]` guard.
- `MCPServerAddRequest` has no mutual-exclusion validation between `command`/`args` (stdio) and `url` (HTTP), so a caller could supply both.