---
{
  "title": "Tool Policy System: Profile-Based Access Control for Agent Tools",
  "summary": "The tool policy module implements a layered access control system that determines which tools an agent backend is allowed to use, using named profiles, logical tool groups, and explicit allow/deny lists with a fixed precedence order. It prevents misconfiguration from silently granting full access and supports fine-grained control over MCP server tools using wildcard patterns.",
  "concepts": [
    "ToolPolicy",
    "tool profiles",
    "allow/deny lists",
    "tool groups",
    "MCP tools",
    "access control",
    "group:fs",
    "group:mcp",
    "TOOL_PROFILES",
    "precedence rules",
    "issue #889"
  ],
  "categories": [
    "tools",
    "security",
    "access control",
    "agent runtime"
  ],
  "source_docs": [
    "e29823a5235538a8"
  ],
  "backlinks": null,
  "word_count": 471,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports multiple agent backends (coding, research, consumer automation) and each should operate with the minimum set of tools it needs -- not the entire registry. The `policy.py` module provides this access control through `ToolPolicy`, a class that evaluates whether any given tool name is permitted under a specific configuration.

## Precedence Rules

The system defines a clear three-tier precedence (highest to lowest):

1. **`tools_deny`** -- a deny list that always wins. Even if a tool appears in the allow list or the active profile, it will be blocked.
2. **`tools_allow`** -- an explicit allow list. When non-empty, only these tools (plus the profile's tools) are available.
3. **`tool_profile`** -- a named preset that defines a baseline set.

This precedence prevents the classic security mistake of "allow wins over deny."

## Tool Groups

Rather than listing dozens of individual tool names, administrators can reference logical groups like `group:fs`, `group:memory`, or `group:browser`. The `TOOL_GROUPS` dictionary maps each group name to its member tool names. Adding a new filesystem tool only requires updating the `group:fs` entry.

The `group:mcp` group is special: it has an empty member list because MCP tool names are dynamic. The group name itself is kept as a sentinel value so that `is_mcp_server_allowed` and `is_mcp_tool_allowed` can check for it.

## Built-in Profiles

Three profiles ship out of the box:

```python
TOOL_PROFILES = {
    "minimal": {"allow": ["group:memory", "group:sessions", "group:explorer"]},
    "coding":  {"allow": ["group:fs", "group:shell", "group:packages", "group:memory", "group:explorer"]},
    "full":    {},  # No restrictions
}
```

The `full` profile's empty allow dict means `_allowed_set` resolves to an empty Python set, which the policy interprets as "no restrictions."

## MCP Tool Handling

MCP servers are namespaced as `mcp:<server>:<tool>`. The policy handles three levels of MCP granularity:

- **`is_mcp_server_allowed(server_name)`** -- checks whether an entire MCP server is permitted (wildcard `mcp:<server>:*`).
- **`is_mcp_tool_allowed(server_name, tool_name)`** -- checks a specific tool on a specific server, checking specific name, server wildcard, and group sentinel in order.

This means an administrator can allow all tools from one MCP server while denying another, or block a specific dangerous tool.

## The Issue #889 Fix

A critical bug fix is documented in `_resolve()`: previously, an unrecognized profile name would silently fall back to `set()`, which is equivalent to the `full` profile (no restrictions). A typo in `tool_profile` would therefore grant full tool access. The fix raises `ValueError` for unknown profiles instead, making misconfiguration immediately visible rather than silently insecure.

```python
def _resolve(self) -> set[str]:
    profile_set = self.resolve_profile(self.profile)  # raises ValueError on unknown
    explicit = self._expand_names(self._allow_raw)
    return profile_set | explicit
```

## Known Gaps

The `group:mcp` entry in `TOOL_GROUPS` is a placeholder with no static members. This means there is no way to enumerate "all MCP tools" for policy auditing -- a tool that iterates `allowed_tool_names` would not see dynamic MCP tools. Future work could integrate MCP server discovery into group resolution.