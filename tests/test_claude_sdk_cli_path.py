# tests/test_claude_sdk_cli_path.py
# Created: 2026-09-27 (chore/bump-claude-agent-sdk) - pins the
# ``claude_sdk_cli_path`` setting. The SDK always prefers the Claude Code CLI
# bundled in its wheel over the one on PATH, so a newer model the bundled CLI
# does not know ("Claude Code 2.1.276 does not support this model; version
# 2.1.280 or newer is required") stayed broken even with a current `claude`
# installed. The setting hands an explicit binary to
# ``ClaudeAgentOptions(cli_path=...)``; unset, the bundled CLI is used as before.
# Reuses the ``_build_options`` harness from test_claude_sdk_model_override.py.

from __future__ import annotations

import pytest

from tests.test_claude_sdk_model_override import _build, _make_sdk, _make_settings


@pytest.mark.asyncio
async def test_a_configured_cli_path_reaches_the_sdk_options():
    sdk = _make_sdk(_make_settings(claude_sdk_cli_path=r"C:\Users\me\.local\bin\claude.exe"))
    kwargs = await _build(sdk)
    assert kwargs["cli_path"] == r"C:\Users\me\.local\bin\claude.exe"


@pytest.mark.asyncio
async def test_surrounding_whitespace_is_trimmed():
    sdk = _make_sdk(_make_settings(claude_sdk_cli_path="  /usr/local/bin/claude  "))
    kwargs = await _build(sdk)
    assert kwargs["cli_path"] == "/usr/local/bin/claude"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "   "])
async def test_unset_keeps_the_bundled_cli(value):
    """No ``cli_path`` key at all, so the SDK's own lookup (bundled first) runs."""
    sdk = _make_sdk(_make_settings(claude_sdk_cli_path=value))
    kwargs = await _build(sdk)
    assert "cli_path" not in kwargs


def test_the_setting_reads_from_the_environment(monkeypatch):
    from pocketpaw.config import Settings

    monkeypatch.setenv("POCKETPAW_CLAUDE_SDK_CLI_PATH", "/opt/claude/bin/claude")
    assert Settings().claude_sdk_cli_path == "/opt/claude/bin/claude"
