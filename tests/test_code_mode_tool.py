# Tests for the CodeModeTool public surface — cap clamping + tenancy resolution.
# Created: 2026-06-16 (feat/code-mode-ptc) — Programmatic Tool Calling v1.

from __future__ import annotations

import pytest

from pocketpaw.tools.code_mode import tool as tool_mod
from pocketpaw.tools.code_mode.tool import CodeModeTool

pytestmark = pytest.mark.asyncio


async def test_timeout_and_max_calls_are_clamped_to_ceilings(monkeypatch):
    """A script arg can lower the caps but never raise them above the ceilings."""
    captured: dict[str, int] = {}

    class _Result:
        stdout = "ok"
        stderr = ""
        exit_code = 0
        timed_out = False
        tool_calls = 0
        rejected_calls: list[str] = []

    async def _fake_run(
        *, registry, script, workspace_id, user_id, max_calls, timeout_s, stdout_cap
    ):
        captured["max_calls"] = max_calls
        captured["timeout_s"] = timeout_s
        return _Result()

    monkeypatch.setattr(tool_mod, "run_code_mode", _fake_run)
    # Avoid building the real registry (cost + noise) — return an empty one.
    monkeypatch.setattr(tool_mod, "_build_read_safe_registry", lambda: None)

    t = CodeModeTool()
    # Ask for absurdly high caps — they must be clamped to the ceilings (30 / 50).
    await t.execute(script="print(1)", timeout_s=9999, max_calls=9999)
    assert captured["timeout_s"] == 30
    assert captured["max_calls"] == 50


async def test_lower_caps_are_respected(monkeypatch):
    captured: dict[str, int] = {}

    async def _fake_run(
        *, registry, script, workspace_id, user_id, max_calls, timeout_s, stdout_cap
    ):
        captured["max_calls"] = max_calls
        captured["timeout_s"] = timeout_s

        class _R:
            stdout = "ok"
            stderr = ""
            exit_code = 0
            timed_out = False
            tool_calls = 0
            rejected_calls: list[str] = []

        return _R()

    monkeypatch.setattr(tool_mod, "run_code_mode", _fake_run)
    monkeypatch.setattr(tool_mod, "_build_read_safe_registry", lambda: None)

    t = CodeModeTool()
    await t.execute(script="print(1)", timeout_s=5, max_calls=3)
    assert captured["timeout_s"] == 5
    assert captured["max_calls"] == 3


async def test_explicit_tenancy_kwargs_win_over_env(monkeypatch):
    captured: dict[str, str] = {}

    async def _fake_run(
        *, registry, script, workspace_id, user_id, max_calls, timeout_s, stdout_cap
    ):
        captured["workspace_id"] = workspace_id
        captured["user_id"] = user_id

        class _R:
            stdout = "ok"
            stderr = ""
            exit_code = 0
            timed_out = False
            tool_calls = 0
            rejected_calls: list[str] = []

        return _R()

    monkeypatch.setattr(tool_mod, "run_code_mode", _fake_run)
    monkeypatch.setattr(tool_mod, "_build_read_safe_registry", lambda: None)
    monkeypatch.setenv("POCKETPAW_WORKSPACE_ID", "env-ws")
    monkeypatch.setenv("POCKETPAW_USER_ID", "env-user")

    t = CodeModeTool()
    # Explicit kwargs must win over the env values.
    await t.execute(script="print(1)", workspace_id="explicit-ws", user_id="explicit-user")
    assert captured["workspace_id"] == "explicit-ws"
    assert captured["user_id"] == "explicit-user"


async def test_tenancy_falls_back_to_env(monkeypatch):
    captured: dict[str, str] = {}

    async def _fake_run(
        *, registry, script, workspace_id, user_id, max_calls, timeout_s, stdout_cap
    ):
        captured["workspace_id"] = workspace_id
        captured["user_id"] = user_id

        class _R:
            stdout = "ok"
            stderr = ""
            exit_code = 0
            timed_out = False
            tool_calls = 0
            rejected_calls: list[str] = []

        return _R()

    monkeypatch.setattr(tool_mod, "run_code_mode", _fake_run)
    monkeypatch.setattr(tool_mod, "_build_read_safe_registry", lambda: None)
    monkeypatch.setenv("POCKETPAW_WORKSPACE_ID", "env-ws")
    monkeypatch.setenv("POCKETPAW_USER_ID", "env-user")

    t = CodeModeTool()
    await t.execute(script="print(1)")
    assert captured["workspace_id"] == "env-ws"
    assert captured["user_id"] == "env-user"


async def test_empty_script_rejected():
    t = CodeModeTool()
    out = await t.execute(script="   ")
    assert "non-empty" in out
