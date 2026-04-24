---
{
  "title": "Tool Policy System: Profile-Based Access Control and MCP Server Authorization",
  "summary": "The tool policy system (`tools/policy.py`) enforces which tools an agent instance is allowed to use, based on named profiles (`minimal`, `coding`, `full`), explicit allow/deny lists, and group references. Tests cover profile resolution, group expansion, allow/deny precedence, ToolRegistry integration, and MCP server authorization — including a critical fix for fail-closed behavior on unknown profile names.",
  "concepts": [
    "ToolPolicy",
    "TOOL_GROUPS",
    "profile",
    "allow_deny",
    "group_expansion",
    "ToolRegistry",
    "MCP",
    "fail_closed",
    "is_tool_allowed",
    "filter_tool_names",
    "minimal_profile",
    "coding_profile"
  ],
  "categories": [
    "tool-system",
    "security",
    "testing",
    "test"
  ],
  "source_docs": [
    "96ce1c9ecd8d810a"
  ],
  "backlinks": null,
  "word_count": 507,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's tool policy system is the authorization layer that decides whether a given tool can be called in a given deployment context. A `ToolPolicy` object is constructed from settings (profile name, optional explicit allow list, optional deny list) and consulted by both the `ToolRegistry` (for execution) and the tool bridge (for registration).

## Profile System

Three built-in profiles cover common deployment scenarios:

- **`minimal`**: Only memory and session management tools (`remember`, `recall`, `forget`, session operations). Suitable for read-only or assistant contexts where shell and file access would be inappropriate.
- **`coding`**: Adds filesystem (`read_file`, `write_file`, `edit_file`, `list_dir`, `directory_tree`) and shell (`shell`, `run_python`) on top of memory. Excludes browser.
- **`full`**: No restrictions — represented internally as an empty allowed set (empty = no whitelist = everything allowed).

## Fail-Closed on Unknown Profiles (Issue #889)

A critical security fix: before issue #889, an unknown profile name silently fell back to `full`, lifting all tool restrictions on a simple typo. Now `ToolPolicy.resolve_profile` raises `ValueError` for unrecognized names, and `ToolPolicy.__init__` propagates the error:

```python
def test_unknown_profile_raises(self):
    with pytest.raises(ValueError, match="Unknown tool profile"):
        ToolPolicy(profile="nonexistent_profile")
```

This is a fail-closed pattern: a misconfigured deployment gets no tools rather than all tools.

## Group References

Tool names can be specified individually or as `group:<name>` references that expand to a set of tool names. Groups are defined in `TOOL_GROUPS` with the `group:` prefix convention. `_expand_names` resolves these references, and unknown group names are kept as literal strings (rather than silently dropped or erroring) to allow future group definitions without breaking existing configs.

Group keys must always carry the `group:` prefix — tested by `test_group_keys_prefixed` — ensuring the namespace is unambiguous.

## Allow/Deny Precedence

The policy evaluation order is:
1. If the tool is in the deny list: **blocked** (highest priority)
2. If the profile is `full` (empty allowed set): **allowed**
3. If the tool is in the profile's allowed set or the explicit allow list: **allowed**
4. Otherwise: **blocked**

This means `deny` always wins, even if the same tool appears in the explicit `allow` list:

```python
def test_deny_overrides_explicit_allow(self):
    policy = ToolPolicy(profile="minimal", allow=["shell"], deny=["shell"])
    assert policy.is_tool_allowed("shell") is False
```

## ToolRegistry Integration

The `ToolRegistry` accepts a `ToolPolicy` at construction time and uses it in two places:
- `get_definitions()`: Filters the schema list before returning, so denied tools never appear in OpenAI/Anthropic function call lists.
- `execute()`: Blocks execution with an `"not allowed"` error message rather than running the tool, providing a second enforcement layer.

The `allowed_tool_names` property exposes the filtered set while `tool_names` still shows all registered tools — useful for diagnostics.

## MCP Server Authorization

The policy system also governs MCP (Model Context Protocol) servers using a `mcp:<server>:<tool>` notation:
- `mcp:dangerous:*` blocks all tools from the `dangerous` server
- `mcp:fs:delete_file` blocks only that specific tool
- `group:mcp` blocks all MCP servers entirely
- `mcp:fs:*` in the allow list grants a specific server to a minimal profile

This granularity allows operators to compose precise access policies for multi-server MCP deployments.

## Known Gaps

None flagged. The policy system is well-tested across all documented scenarios.