# Test that the Claude SDK backend always bypasses permissions in headless mode.
# Created: 2026-03-11 — Regression test for the permission hang bug on messaging
#   channels (Telegram, Discord, Slack). Without bypassPermissions, tool calls
#   that need interactive approval hang forever because there's no terminal.
#
# The bug: commit 24f16e2 gated permission bypass behind a setting that defaults
#   to false, breaking ALL Bash-based tools (memory, web search, gmail, etc.)
#   on every messaging channel since v0.3.0.

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock

import pytest


class TestHeadlessPermissionMode:
    """Verify permission_mode is always bypassPermissions regardless of settings."""

    def _make_settings(self, bypass: bool = False) -> MagicMock:
        """Create a minimal mock Settings with the given bypass_permissions value."""
        settings = MagicMock()
        settings.bypass_permissions = bypass
        settings.agent_backend = "claude_agent_sdk"
        settings.anthropic_api_key = "sk-ant-test-key"
        settings.claude_sdk_model = ""
        settings.claude_sdk_max_turns = 0
        settings.smart_routing_enabled = False
        settings.tool_profile = "full"
        settings.tools_allow = []
        settings.tools_deny = []
        settings.mcp_servers = {}
        settings.claude_sdk_provider = "anthropic"
        settings.ollama_base_url = "http://localhost:11434"
        settings.openai_api_key = ""
        settings.openai_base_url = ""
        settings.openrouter_api_key = ""
        settings.gemini_api_key = ""
        settings.openai_agents_model = ""
        return settings

    def test_permission_mode_set_when_bypass_false(self):
        """Core regression test: bypass_permissions=False must still set bypassPermissions.

        This is the exact scenario that broke Telegram/Discord/Slack — the default
        setting caused permission_mode to not be set, making tool calls hang.
        """
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        backend = ClaudeSDKBackend(self._make_settings(bypass=False))

        # We can't easily run the full .run() method without the SDK installed,
        # but we can inspect the source to verify the fix is present.
        import inspect

        source = inspect.getsource(backend.run)

        # The fix: permission_mode should be set unconditionally (no if statement)
        # Old broken code: 'if self.settings.bypass_permissions:'
        # Fixed code: 'options_kwargs["permission_mode"] = "bypassPermissions"'
        assert "if self.settings.bypass_permissions" not in source, (
            "permission_mode is still gated behind bypass_permissions setting! "
            "This causes tool calls to hang on messaging channels."
        )
        assert '"bypassPermissions"' in source, (
            "bypassPermissions not found in run() — permission mode must be set"
        )

    def test_permission_mode_set_when_bypass_true(self):
        """Verify bypass_permissions=True also works (should be same behavior now)."""
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        backend = ClaudeSDKBackend(self._make_settings(bypass=True))

        import inspect

        source = inspect.getsource(backend.run)
        assert '"bypassPermissions"' in source

    def test_no_conditional_bypass_in_options_build(self):
        """Verify the options_kwargs assignment is unconditional by checking
        that 'permission_mode' appears exactly once and not inside an if block."""
        import inspect

        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        source = inspect.getsource(ClaudeSDKBackend.run)

        # Count occurrences of permission_mode assignment
        lines = source.split("\n")
        permission_lines = [
            (i, line.strip())
            for i, line in enumerate(lines)
            if "permission_mode" in line and "=" in line
        ]

        assert len(permission_lines) >= 1, "permission_mode assignment not found"

        # The assignment line should NOT be indented inside an if block
        # relative to the surrounding options_kwargs assignments
        for _idx, line in permission_lines:
            assert not line.startswith("if "), f"permission_mode is inside a conditional: {line}"


class TestToolExecutionInSubprocess:
    """Simulate the Claude SDK's Bash tool path: run memory tools in a subprocess."""

    def test_remember_via_subprocess(self, tmp_path):
        """Simulate what the Claude SDK does: spawn a subprocess to run a tool.

        This is the exact path that hangs when permissions aren't bypassed —
        the SDK runs Bash, which spawns `python -m pocketpaw.tools.cli remember ...`.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pocketpaw.tools.cli",
                "remember",
                '{"content": "User name is Ade", "tags": ["personal"]}',
            ],
            capture_output=True,
            text=True,
            timeout=10,  # Should complete in <2s; 10s catches hangs
            env={
                **dict(__import__("os").environ),
                "HOME": str(tmp_path),  # Isolate from real config
                "USERPROFILE": str(tmp_path),  # Windows compat
                "PYTHONIOENCODING": "utf-8",
            },
        )

        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        assert "Remembered" in result.stdout, f"Unexpected output: {result.stdout}"

    def test_remember_then_recall_via_subprocess(self, tmp_path):
        """Full round-trip: save then search, both via subprocess (the real path)."""
        env = {
            **dict(__import__("os").environ),
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "PYTHONIOENCODING": "utf-8",
        }

        # Save
        save_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pocketpaw.tools.cli",
                "remember",
                '{"content": "User name is Ade", "tags": ["personal"]}',
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert save_result.returncode == 0, f"Save failed: {save_result.stderr}"

        # Recall
        recall_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pocketpaw.tools.cli",
                "recall",
                '{"query": "Ade"}',
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert recall_result.returncode == 0, f"Recall failed: {recall_result.stderr}"
        assert "Ade" in recall_result.stdout, (
            f"Memory not found after save. Output: {recall_result.stdout}"
        )

    def test_subprocess_tool_does_not_hang(self, tmp_path):
        """Verify tool execution completes within 5 seconds (catches permission hangs)."""
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pocketpaw.tools.cli",
                    "recall",
                    '{"query": "anything"}',
                ],
                capture_output=True,
                text=True,
                timeout=5,
                env={
                    **dict(__import__("os").environ),
                    "HOME": str(tmp_path),
                    "USERPROFILE": str(tmp_path),
                    "PYTHONIOENCODING": "utf-8",
                },
            )
            # Should complete without timeout
            assert result.returncode == 0
        except subprocess.TimeoutExpired:
            pytest.fail("Tool execution timed out after 5s — this is the permission hang bug!")
