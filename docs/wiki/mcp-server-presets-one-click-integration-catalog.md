---
{
  "title": "MCP Server Presets — One-Click Integration Catalog",
  "summary": "`presets.py` maintains a curated catalog of pre-configured MCP server templates (`MCPPreset`) that users can install from the PocketPaw dashboard by pasting a single API key, eliminating the need to understand MCP server configuration details for common integrations.",
  "concepts": [
    "MCPPreset",
    "EnvKeySpec",
    "one-click install",
    "preset catalog",
    "transform",
    "API key",
    "to_server_config",
    "category filter",
    "dashboard integration"
  ],
  "categories": [
    "MCP Integration",
    "Developer Experience"
  ],
  "source_docs": [
    "27abdcd085ed928f"
  ],
  "backlinks": null,
  "word_count": 492,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Configuring an MCP server from scratch requires knowing the executable name, argument order, environment variable names, and transport type. For popular integrations, this friction is unnecessary. `presets.py` eliminates it by shipping a catalog of ready-to-use templates.

## Key Types

### EnvKeySpec

```python
@dataclass
class EnvKeySpec:
    key: str           # e.g. "GITHUB_PERSONAL_ACCESS_TOKEN"
    label: str         # e.g. "Personal Access Token"
    required: bool = True
    placeholder: str = ""
    secret: bool = True
    transform: str = ""  # e.g. '{"Authorization": "Bearer {value}"}'
```

`transform` handles the common pattern where a raw token must be wrapped in a structured value before being passed to the server. For example, some MCP servers expect an Authorization header value rather than a raw token. The dashboard substitutes `{value}` with the user's input and writes the result to the env dict — the user only sees a simple 'Token' field.

### MCPPreset

```python
@dataclass
class MCPPreset:
    id: str        # "github"
    name: str      # "GitHub"
    description: str
    icon: str      # lucide icon name
    category: str  # "dev" | "productivity" | "data" | "search" | "devops"
```

`to_server_config(env_values)` converts a preset and user-supplied env values into an `MCPServerConfig` ready for `save_mcp_config`. This is the bridge between the dashboard UI and the config layer.

## Catalog Categories

| Category | Example Integrations |
|----------|---------------------|
| `dev` | GitHub, GitLab |
| `productivity` | Notion, Linear, Google Workspace |
| `data` | databases, analytics |
| `search` | web search tools |
| `devops` | CI/CD, infrastructure |

## Design Rationale

Keeping presets in Python (rather than a JSON config file) means the `transform` logic, default values, and validation rules are co-located and type-checked. Adding a new preset is a single dataclass instantiation — no separate schema to update.

The `get_presets_by_category` helper exists because the dashboard renders presets grouped by category. Filtering at the data layer avoids shipping the full catalog to the frontend.

## User Experience Goal

The preset system is designed so that a non-technical user can add a GitHub integration in under 30 seconds: open the MCP tab, click GitHub, paste a personal access token, and click Install. The `EnvKeySpec.label` field controls what the dashboard shows ('Personal Access Token' instead of 'GITHUB_PERSONAL_ACCESS_TOKEN'), and `EnvKeySpec.placeholder` provides example values. The `secret: bool = True` flag tells the dashboard to render the input as a password field.

## to_server_config

`MCPPreset.to_server_config(env_values: dict[str, str]) -> MCPServerConfig` is the key method: it takes the user's raw inputs, applies any `transform` substitutions, and produces a fully-populated `MCPServerConfig` ready to be saved via `save_mcp_config`. This means the entire install flow is: look up preset, collect env values from the user, call `to_server_config`, call `save_mcp_config`, then call `MCPManager.start_server`.

## Known Gaps

Presets are hardcoded; there is no remote preset registry or update mechanism. Adding support for a new third-party MCP server requires a code change and a new PocketPaw release. The `transform` field uses simple string substitution; complex transforms (base64 encoding, nested JSON) are not expressible without custom code.