# tests/test_claude_sdk_allow_mcp_tools.py — Per-mode restrictive MCP allow-list.
#
# Companion to the additive allow_sdk_tools gate (test_claude_sdk_allow_sdk_tools)
# and the deny gate. A chat mode can hand the backend an ``allow_mcp_tool_ids``
# set so it carries ONLY its own MCP tools instead of every server's ids,
# keeping the agent context lean. Invariants:
#   1. built-in SDK tools (Read/Write/Bash/...) are NEVER filtered — only mcp__*.
#   2. the pocket-creation grant, ripple widgets, and always-allowed servers
#      (connectors + pocket lifecycle) survive even when not named — so every
#      mode can still make pockets, render UI, and use connectors.
#   3. None = no restriction (broad surfaces keep every tool).
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
    """Mirror the restrictive filter the backend runs inside ``run()``."""
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


def test_run_accepts_allow_mcp_kwarg_defaulting_none() -> None:
    params = inspect.signature(ClaudeSDKBackend.run).parameters
    assert "allow_mcp_tool_ids" in params
    assert params["allow_mcp_tool_ids"].default is None, (
        "allow_mcp_tool_ids must default to None so unscoped surfaces keep every tool"
    )


def test_none_allow_keeps_all_tools() -> None:
    assert _apply_allow(_FULL_TOOLSET, None) == _FULL_TOOLSET


def test_allow_scopes_mcp_but_keeps_general_everywhere() -> None:
    """A foresight-style allow set keeps foresight tools + built-ins + the
    general-everywhere set, and drops other modes' specialized tools."""
    gated = _apply_allow(_FULL_TOOLSET, frozenset({_SCENARIO_RUN}))

    assert _SCENARIO_RUN in gated  # the mode's own tool
    assert _TASKS not in gated  # other-mode specialized → dropped
    assert _PUBLISH not in gated
    for builtin in ("Read", "Write", "Bash", "Skill", "WebSearch"):
        assert builtin in gated  # built-ins never filtered
    assert _SPECIALIST_CREATE in gated  # pocket-creation grant
    assert _PLAN_POCKET in gated  # pocket-creation grant
    assert _SPECIALIST_EDIT in gated  # pocket lifecycle (always-allowed server)
    assert _POCKET_GET in gated  # pocket read (always-allowed server)
    assert _WIDGET_SPEC in gated  # ripple rendering
    assert _COMPOSIO in gated  # connectors (always-allowed server)


def test_empty_allow_keeps_only_general() -> None:
    """An empty allow set (e.g. Files mode) = general-everywhere only."""
    gated = _apply_allow(_FULL_TOOLSET, frozenset())
    assert _SCENARIO_RUN not in gated
    assert _TASKS not in gated
    assert _SPECIALIST_CREATE in gated
    assert _COMPOSIO in gated
    assert "Write" in gated


def test_helpers_present() -> None:
    assert _SPECIALIST_CREATE in POCKET_CREATION_GRANT
    assert "composio" in ALWAYS_ALLOWED_MCP_SERVERS
    assert _mcp_server_of(_COMPOSIO) == "composio"
    assert _mcp_server_of("Read") == ""
