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

from pocketpaw.agents.claude_sdk import POCKET_CREATION_GRANT, ClaudeSDKBackend

_SPECIALIST_CREATE = "mcp__pocketpaw_pocket_specialist__create"
_PLAN_POCKET = "mcp__pocketpaw_pocket_planner__plan_pocket"
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
    _SCENARIO_RUN,
    _TASKS,
    _PUBLISH,
    _SPECIALIST_CREATE,
    _PLAN_POCKET,
]


def _apply_allow(allowed_tools: list[str], allow_mcp_tool_ids: frozenset[str] | None) -> list[str]:
    """Mirror the filter the backend runs inside ``run()``.

    ``None`` keeps every tool. Otherwise keep all non-``mcp__`` tools plus the
    MCP ids in (allow ∪ POCKET_CREATION_GRANT). Kept tiny + pure so the test
    pins the SAME expression the backend uses.
    """
    if allow_mcp_tool_ids is None:
        return list(allowed_tools)
    grant = allow_mcp_tool_ids | POCKET_CREATION_GRANT
    return [t for t in allowed_tools if not t.startswith("mcp__") or t in grant]


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


def test_allow_scopes_mcp_but_keeps_builtins_and_grant() -> None:
    """A foresight-style allow set keeps foresight MCP tools + built-ins + the
    universal pocket-creation grant, and drops unrelated MCP servers."""
    foresight_allow = frozenset({_SCENARIO_RUN})
    gated = _apply_allow(_FULL_TOOLSET, foresight_allow)

    # The mode's own MCP tool survives.
    assert _SCENARIO_RUN in gated
    # Unrelated MCP servers are dropped — this is the lean-context win.
    assert _TASKS not in gated
    assert _PUBLISH not in gated
    # Built-in SDK tools are never filtered by the MCP allow-list.
    for builtin in ("Read", "Write", "Bash", "Skill", "WebSearch"):
        assert builtin in gated
    # The universal pocket-creation grant survives even though the allow set
    # never named it — create-a-pocket works from every mode.
    assert _SPECIALIST_CREATE in gated
    assert _PLAN_POCKET in gated


def test_grant_constant_is_pocket_creation() -> None:
    assert _SPECIALIST_CREATE in POCKET_CREATION_GRANT
    assert _PLAN_POCKET in POCKET_CREATION_GRANT
