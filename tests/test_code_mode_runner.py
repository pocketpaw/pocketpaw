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
    # passes. The tool must DECLARE workspace_id for the bridge to inject it
    # (a tenancy-blind tool never gets force-fed one).
    class _EchoTenancy(_ReadTool):
        @property
        def parameters(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "workspace_id": {"type": "string"},
                },
                "required": ["path"],
            }

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


async def test_secret_scrub_covers_fernet_jwt_bearer(monkeypatch):
    monkeypatch.setenv("FERNET_KEYS", "x")  # also matches KEY, belt-and-braces
    monkeypatch.setenv("MY_JWT_THING", "y")
    monkeypatch.setenv("BEARER_HEADER", "z")
    script = """
import os
leaked = [k for k in os.environ if k in ("FERNET_KEYS", "MY_JWT_THING", "BEARER_HEADER")]
print("LEAKED:" + ",".join(sorted(leaked)))
"""
    result = await run_code_mode(registry=_registry(), script=script, timeout_s=15)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "LEAKED:"


async def test_home_redirected_to_throwaway_dir():
    # HOME must NOT be the host user's home — it points at the run's work_dir.
    import os

    host_home = os.environ.get("HOME", "")
    script = """
import os
print("HOME=" + os.environ.get("HOME", ""))
"""
    result = await run_code_mode(registry=_registry(), script=script, timeout_s=15)
    assert result.exit_code == 0, result.stderr
    child_home = result.stdout.strip().removeprefix("HOME=")
    assert child_home != host_home
    assert "pocketpaw-code-mode" in child_home


async def test_fallback_socket_is_cleaned_up(monkeypatch):
    # Force the pathological-path branch in _socket_path so the socket lands
    # OUTSIDE work_dir (under the temp root), then assert the finally block
    # unlinks it — no stale socket leaks. Use a SHORT temp-root path so the
    # AF_UNIX length limit (~104 chars) isn't hit by the test itself.
    import tempfile
    import uuid
    from pathlib import Path

    from pocketpaw.tools.code_mode import runner as runner_mod

    captured: dict[str, str] = {}
    fallback = str(Path(tempfile.gettempdir()) / f"pcm-test-{uuid.uuid4().hex[:8]}.sock")

    def _force_fallback(work_dir: Path) -> str:
        captured["path"] = fallback
        return fallback

    monkeypatch.setattr(runner_mod, "_socket_path", _force_fallback)
    script = "print('done')"
    result = await run_code_mode(registry=_registry(), script=script, timeout_s=15)
    assert result.exit_code == 0, result.stderr
    # The fallback socket must be gone after the run.
    assert "path" in captured
    assert not Path(captured["path"]).exists()
