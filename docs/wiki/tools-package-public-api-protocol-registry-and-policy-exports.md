---
{
  "title": "Tools Package Public API: Protocol, Registry, and Policy Exports",
  "summary": "The `pocketpaw/tools/__init__.py` re-exports the three foundational primitives of PocketPaw's tool system: the `ToolProtocol`/`BaseTool` interface, the `ToolRegistry`, and the `ToolPolicy` with its grouping constants. This single import surface means tool consumers never need to know the internal module layout.",
  "concepts": [
    "ToolProtocol",
    "BaseTool",
    "ToolDefinition",
    "ToolRegistry",
    "ToolPolicy",
    "TOOL_GROUPS",
    "TOOL_PROFILES",
    "trust level",
    "tool system",
    "public API"
  ],
  "categories": [
    "tool-system",
    "package-structure"
  ],
  "source_docs": [
    "4f7946b3149ea825"
  ],
  "backlinks": null,
  "word_count": 342,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's tool system has three distinct concerns: what a tool looks like (`ToolProtocol`/`BaseTool`/`ToolDefinition`), where tools are stored and looked up (`ToolRegistry`), and which tools are active for a given agent profile (`ToolPolicy`/`TOOL_GROUPS`/`TOOL_PROFILES`). The `__init__.py` unifies all three into a single import surface.

## Why Consolidate Here?

Without this consolidation, a caller building a custom tool would need to know that the base class lives in `pocketpaw.tools.protocol`, that registration happens via `pocketpaw.tools.registry`, and that trust levels are defined in `pocketpaw.tools.policy`. These are implementation details that should not leak into user-facing code. The `__init__.py` provides the stable contract:

```python
from pocketpaw.tools import BaseTool, ToolRegistry, ToolPolicy, TOOL_GROUPS
```

This import remains valid even if the internal modules are reorganized in the future.

## Exported Symbols

- **`ToolProtocol`** — the structural typing protocol; any class with matching `name`, `description`, `parameters`, and `execute` attributes satisfies it without inheriting from `BaseTool`.
- **`BaseTool`** — the concrete abstract base class with shared utilities (`_error()`, `_success()` formatters) that most built-in tools extend.
- **`ToolDefinition`** — the serializable dataclass that describes a tool to the LLM (name, description, parameter schema). Used when building the LLM's tool list for a request.
- **`ToolRegistry`** — the runtime container that maps tool names to instances and handles deduplication on registration.
- **`ToolPolicy`** — the class encapsulating which tools are enabled for a given configuration, respecting trust level constraints.
- **`TOOL_GROUPS`** — a constant dictionary mapping group names (e.g., `"web"`, `"files"`, `"memory"`) to lists of tool names.
- **`TOOL_PROFILES`** — named profiles (e.g., `"default"`, `"minimal"`, `"power"`) that pre-select groups for common deployment scenarios.

## Trust Level Integration

`ToolPolicy` enforces trust levels at resolution time. When a policy is asked for the tool list for a given session, it filters out tools whose `trust_level` exceeds the session's configured maximum. This means a session configured for `"high"` trust will not receive `"critical"`-level tools like `DelegateToClaudeCodeTool`, even if those tools are registered in the registry.

## Known Gaps

None identified. This file is intentionally thin — its sole job is re-exporting, and it does that cleanly.