# End-to-end tests for the Code Mode runner + tool.
# Created: 2026-06-16 (feat/code-mode-ptc) — Programmatic Tool Calling v1.
#
# Proves the full loop: a script imports paw_tools, chains read-safe calls, and
# ONLY its final stdout returns (intermediate tool results are discarded). Plus
# timeout, max-calls, and write-tool rejection from inside a real child process.

from __future__ import annotations

from typing import Any

import pytest

from pocketpaw.tools.code_mode.runner import run_code_mode
from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.registry import ToolRegistry

pytestmark = pytest.mark.asyncio


class _ReadTool(BaseTool):
    """Allowlisted read tool returning a deterministic, recognizable string."""

    def __init__(self, name: str = "read_file", trust: str = "standard") -> None:
        self._name = name
        self._trust = trust

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "read"

    @property
    def trust_level(self) -> str:
        return self._trust

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    async def execute(self, path: str = "", **_extra: Any) -> str:
        return f"CONTENTS_OF::{path}"


def _registry(tool: BaseTool | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(tool or _ReadTool())
    return reg


async def test_only_final_stdout_returns():
    # The script makes two read calls but prints ONLY a derived final answer.
    # The intermediate INTERMEDIATE_MARKER tokens must NOT appear in the result.
    script = """
import paw_tools
a = paw_tools.read_file(path="alpha")
b = paw_tools.read_file(path="beta")
# 'a' and 'b' carry CONTENTS_OF::alpha / CONTENTS_OF::beta — never printed raw.
print("FINAL:" + str(len(a) + len(b)))
"""
    result = await run_code_mode(registry=_registry(), script=script, timeout_s=15)
    assert result.exit_code == 0, result.stderr
    assert result.timed_out is False
    assert result.stdout.strip().startswith("FINAL:")
    # Intermediate tool results were discarded — they never reach the caller.
    assert "CONTENTS_OF::alpha" not in result.stdout
    assert "CONTENTS_OF::beta" not in result.stdout
    assert result.tool_calls == 2


async def test_read_tool_result_is_reachable_in_script():
    script = """
import paw_tools
r = paw_tools.read_file(path="x")
print(r)
"""
    result = await run_code_mode(registry=_registry(), script=script, timeout_s=15)
    assert result.exit_code == 0, result.stderr
    assert "CONTENTS_OF::x" in result.stdout


async def test_write_tool_absent_and_rejected_in_sandbox():
    # Register a write tool. It must NOT have a stub (AttributeError on call),
    # and calling the bridge directly by name must be rejected.
    reg = ToolRegistry()
    reg.register(_ReadTool())  # read_file stub exists
    reg.register(_ReadTool(name="write_file", trust="standard"))  # mutating — excluded
    script = """
import paw_tools
assert not hasattr(paw_tools, "write_file"), "write_file leaked into stubs!"
# Try to call it directly through the transport — must be rejected by the bridge.
try:
    paw_tools._call("write_file", {"path": "/x", "content": "y"})
    print("LEAK")
except RuntimeError as e:
    print("BLOCKED:" + str(e))
"""
    result = await run_code_mode(registry=reg, script=script, timeout_s=15)
    assert result.exit_code == 0, result.stderr
    assert "BLOCKED:" in result.stdout
    assert "read-safe" in result.stdout
    assert "LEAK" not in result.stdout


async def test_timeout_enforced():
    script = """
import time
time.sleep(30)
print("should not get here")
"""
    result = await run_code_mode(registry=_registry(), script=script, timeout_s=2)
    assert result.timed_out is True
    assert "should not get here" not in result.stdout


async def test_max_calls_enforced_in_sandbox():
    # Script tries 5 calls but the budget is 2 — the 3rd raises in-script.
    script = """
import paw_tools
ok = 0
for i in range(5):
    try:
        paw_tools.read_file(path=str(i))
        ok += 1
    except RuntimeError as e:
        print("STOPPED_AT:" + str(ok) + ":" + str(e))
        break
"""
    result = await run_code_mode(registry=_registry(), script=script, timeout_s=15, max_calls=2)
    assert result.exit_code == 0, result.stderr
    assert "STOPPED_AT:2:" in result.stdout
    assert "budget" in result.stdout


async def test_tenancy_threaded_to_bridge():
    # The runner-resolved workspace_id reaches the tool, not anything the script
    # passes. _ReadTool ignores it, so use a tool that echoes tenancy.
    class _EchoTenancy(_ReadTool):
        async def execute(self, path: str = "", workspace_id: str = "", **_e: Any) -> str:
            return f"WS={workspace_id}"

    reg = _registry(_EchoTenancy())
    script = """
import paw_tools
print(paw_tools.read_file(path="x", workspace_id="EVIL"))
"""
    result = await run_code_mode(
        registry=reg, script=script, workspace_id="real-ws", user_id="u", timeout_s=15
    )
    assert result.exit_code == 0, result.stderr
    assert "WS=real-ws" in result.stdout
    assert "EVIL" not in result.stdout


async def test_secrets_scrubbed_from_child_env(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "super-secret-value")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    script = """
import os
leaked = [k for k in os.environ if "API_KEY" in k or k == "DATABASE_URL"]
print("LEAKED:" + ",".join(sorted(leaked)))
"""
    result = await run_code_mode(registry=_registry(), script=script, timeout_s=15)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "LEAKED:"
