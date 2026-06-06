# tests/test_claude_sdk_allow_mcp_tools.py — Per-surface MCP-tool ALLOW-list (OSS).
#
# Created: 2026-06-06 (feat/per-mode-tool-scope) — companion to the threaded
# deny gate (test_claude_sdk_deny_mcp_tools.py). A surface can now hand the
# backend an ``allow_mcp_tool_ids`` set so a chat mode (files, foresight, ...)
# carries only its own MCP tools instead of every server's ids, keeping the
# agent context lean. Two invariants the backend must hold:
#   1. built-in SDK tools (Read/Write/Bash/Skill/Web*) are NEVER filtered — the
#      allow-list only narrows ``mcp__*`` ids, so scoping a mode can't strip the
#      agent's core file/shell tools.
#   2. the universal POCKET_CREATION_GRANT always survives, even when a mode's
#      allow set doesn't name it — "create a pocket" works from every mode.
from __future__ import annotations

import inspect

from pocketpaw.agents.claude_sdk import (
    ALWAYS_ALLOWED_MCP_SERVERS,
    POCKET_CREATION_GRANT,
    ClaudeSDKBackend,
    _mcp_server_of,
)

_SPECIALIST_CREATE = "mcp__pocketpaw_pocket_specialist__create"
_SPECIALIST_EDIT = "mcp__pocketpaw_pocket_specialist__edit"
_PLAN_POCKET = "mcp__pocketpaw_pocket_planner__plan_pocket"
_POCKET_GET = "mcp__pocketpaw_pocket__get_pocket"
_WIDGET_SPEC = "mcp__pocketpaw_widgets__get_widget_spec"
_COMPOSIO = "mcp__composio__GMAIL_SEND_EMAIL"
_SCENARIO_RUN = "mcp__pocketpaw_foresight__run_scenario"
_TASKS = "mcp__pocketpaw_tasks__create_task"
_PUBLISH = "mcp__pocketpaw_sites_manager__publish"

# A broad toolset like /chat would launch with: built-ins + many MCP servers.
_FULL_TOOLSET = [
    "Read",
    "Write",
    "Bash",
    "Skill",
    "WebSearch",
    _WIDGET_SPEC,
    _POCKET_GET,
    _COMPOSIO,
    _SCENARIO_RUN,
    _TASKS,
    _PUBLISH,
    _SPECIALIST_CREATE,
    _SPECIALIST_EDIT,
    _PLAN_POCKET,
]


def _apply_allow(allowed_tools: list[str], allow_mcp_tool_ids: frozenset[str] | None) -> list[str]:
    """Mirror the filter the backend runs inside ``run()``.

    ``None`` keeps every tool. Otherwise keep all non-``mcp__`` tools, the MCP
    ids in (allow ∪ pocket-creation grant ∪ widget tools), and any tool whose
    server is always-allowed (connectors + pocket lifecycle). Kept tiny + pure
    so the test pins the SAME logic the backend uses.
    """
    if allow_mcp_tool_ids is None:
        return list(allowed_tools)
    grant = allow_mcp_tool_ids | POCKET_CREATION_GRANT | {_WIDGET_SPEC}
    return [
        t
        for t in allowed_tools
        if not t.startswith("mcp__")
        or t in grant
        or _mcp_server_of(t) in ALWAYS_ALLOWED_MCP_SERVERS
    ]


def test_run_accepts_allow_kwarg_defaulting_none() -> None:
    params = inspect.signature(ClaudeSDKBackend.run).parameters
    assert "allow_mcp_tool_ids" in params, (
        "ClaudeSDKBackend.run must accept a threaded allow_mcp_tool_ids kwarg"
    )
    assert params["allow_mcp_tool_ids"].default is None, (
        "allow_mcp_tool_ids must default to None so unscoped surfaces keep every tool"
    )


def test_none_allow_keeps_all_tools() -> None:
    """The default (broad surfaces like /chat) leaves the allowlist untouched."""
    assert _apply_allow(_FULL_TOOLSET, None) == _FULL_TOOLSET


def test_allow_scopes_mcp_but_keeps_general_everywhere() -> None:
    """A foresight-style allow set keeps foresight tools + built-ins + the
    'general everywhere' set (pocket lifecycle, ripple widgets, connectors),
    and drops the other modes' specialized tools."""
    foresight_allow = frozenset({_SCENARIO_RUN})
    gated = _apply_allow(_FULL_TOOLSET, foresight_allow)

    # The mode's own MCP tool survives.
    assert _SCENARIO_RUN in gated
    # Other modes' specialized tools are dropped — the lean-context win.
    assert _TASKS not in gated
    assert _PUBLISH not in gated
    # Built-in SDK tools are never filtered by the MCP allow-list.
    for builtin in ("Read", "Write", "Bash", "Skill", "WebSearch"):
        assert builtin in gated
    # General-everywhere survives without being named in the allow set:
    assert _SPECIALIST_CREATE in gated  # pocket creation grant
    assert _PLAN_POCKET in gated  # pocket creation grant
    assert _SPECIALIST_EDIT in gated  # pocket lifecycle (always-allowed server)
    assert _POCKET_GET in gated  # pocket read (always-allowed server)
    assert _WIDGET_SPEC in gated  # ripple rendering
    assert _COMPOSIO in gated  # connectors (always-allowed server)


def test_grant_constant_is_pocket_creation() -> None:
    assert _SPECIALIST_CREATE in POCKET_CREATION_GRANT
    assert _PLAN_POCKET in POCKET_CREATION_GRANT


def test_always_allowed_servers_cover_connectors_and_pocket() -> None:
    assert "composio" in ALWAYS_ALLOWED_MCP_SERVERS
    assert "pocketpaw_pocket" in ALWAYS_ALLOWED_MCP_SERVERS
    assert _mcp_server_of(_COMPOSIO) == "composio"
    assert _mcp_server_of("Read") == ""
