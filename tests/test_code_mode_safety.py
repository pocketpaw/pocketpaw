# Tests for the Code Mode read-safe gate (the predicate stubgen + bridge share).
# Created: 2026-06-16 (feat/code-mode-ptc) — Programmatic Tool Calling v1.
#
# Proves the gate: trust-ceiling rejection, mutation deny-set, allowlist-by-
# construction, and the instinct_pending sentinel detector.

from __future__ import annotations

from typing import Any

import pytest

from pocketpaw.tools.code_mode.safety import (
    READ_SAFE_TOOL_NAMES,
    carries_instinct_pending,
    is_read_safe_name,
    is_read_safe_tool,
    passes_trust_ceiling,
)
from pocketpaw.tools.protocol import BaseTool


class _FakeTool(BaseTool):
    def __init__(self, name: str, trust: str = "standard") -> None:
        self._name = name
        self._trust = trust

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"fake {self._name}"

    @property
    def trust_level(self) -> str:
        return self._trust

    async def execute(self, **params: Any) -> str:
        return "ok"


def test_standard_read_tool_on_allowlist_is_read_safe():
    assert is_read_safe_tool(_FakeTool("read_file", "standard")) is True
    assert is_read_safe_tool(_FakeTool("web_search", "standard")) is True


@pytest.mark.parametrize("trust", ["medium", "high", "critical"])
def test_trust_ceiling_rejects_above_standard(trust):
    # Even an allowlisted name is rejected if trust is above the ceiling.
    tool = _FakeTool("read_file", trust)
    assert passes_trust_ceiling(tool) is False
    assert is_read_safe_tool(tool) is False


def test_fabric_query_high_trust_is_blocked():
    # fabric_query carries trust_level "high" — never read-safe in v1.
    assert is_read_safe_tool(_FakeTool("fabric_query", "high")) is False


def test_write_tool_at_standard_trust_is_denied_by_name():
    # write_file / edit_file are "standard" trust but MUTATE — must be denied.
    assert is_read_safe_tool(_FakeTool("write_file", "standard")) is False
    assert is_read_safe_tool(_FakeTool("edit_file", "standard")) is False
    assert is_read_safe_tool(_FakeTool("create_pocket", "standard")) is False


def test_unknown_tool_fails_closed():
    # An unknown standard tool not on the allowlist is excluded by construction.
    assert is_read_safe_tool(_FakeTool("some_new_tool", "standard")) is False


def test_code_mode_itself_is_not_read_safe():
    # Structural no-recursion: code_mode must never be exposable inside code_mode.
    assert "code_mode" not in READ_SAFE_TOOL_NAMES
    assert is_read_safe_tool(_FakeTool("code_mode", "standard")) is False


def test_instinct_tools_blocked():
    assert is_read_safe_tool(_FakeTool("instinct_propose", "medium")) is False
    assert is_read_safe_tool(_FakeTool("instinct_pending", "high")) is False
    assert is_read_safe_tool(_FakeTool("instinct_audit", "high")) is False


def test_name_gate_matches_allowlist():
    assert is_read_safe_name("read_file") is True
    assert is_read_safe_name("write_file") is False
    assert is_read_safe_name("fabric_query") is False
    assert is_read_safe_name("") is False


def test_instinct_pending_sentinel_detection():
    assert carries_instinct_pending("action queued: instinct_pending") is True
    assert carries_instinct_pending("normal result") is False
    assert carries_instinct_pending("") is False
    assert carries_instinct_pending(None) is False


def test_no_registered_write_tool_leaks_through_real_registry():
    """Against the REAL builtin set, no mutating/gated tool is read-safe.

    Guards against a future builtin landing on the allowlist by accident.
    """
    import importlib

    from pocketpaw.tools.builtin import _LAZY_IMPORTS

    known_mutating = {
        "write_file",
        "edit_file",
        "create_pocket",
        "add_widget",
        "remove_widget",
        "shell",
        "run_python",
        "fabric_query",
        "fabric_create",
        "connector_execute",
        "connector_connect",
        "gmail_send",
        "instinct_propose",
        "code_mode",
    }
    seen = set()
    for _cls_name, (module_path, attr_name) in _LAZY_IMPORTS.items():
        try:
            mod = importlib.import_module(module_path, "pocketpaw.tools.builtin")
            tool = getattr(mod, attr_name)()
        except Exception:
            continue
        if is_read_safe_tool(tool):
            seen.add(tool.name)
    # None of the known mutating/gated tools may be in the read-safe set.
    assert seen.isdisjoint(known_mutating), f"leaked: {seen & known_mutating}"
    # And the read-safe set is a strict subset of the curated allowlist.
    assert seen.issubset(READ_SAFE_TOOL_NAMES)
