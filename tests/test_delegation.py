# Tests for agents/delegation.py and tools/builtin/delegate.py
# Created: 2026-02-07

from unittest.mock import AsyncMock, patch

from pocketclaw.agents.delegation import DelegationResult, ExternalAgentDelegate
from pocketclaw.tools.builtin.delegate import DelegateToClaudeCodeTool

# ---------------------------------------------------------------------------
# DelegationResult
# ---------------------------------------------------------------------------


class TestDelegationResult:
    def test_fields(self):
        r = DelegationResult(agent="claude", output="hello", exit_code=0)
        assert r.agent == "claude"
        assert r.output == "hello"
        assert r.exit_code == 0
        assert r.error == ""

    def test_with_error(self):
        r = DelegationResult(agent="claude", output="", exit_code=1, error="timeout")
        assert r.error == "timeout"


# ---------------------------------------------------------------------------
# ExternalAgentDelegate
# ---------------------------------------------------------------------------


class TestExternalAgentDelegate:
    def test_is_available_not_installed(self):
        with patch("pocketclaw.agents.delegation.shutil.which", return_value=None):
            assert ExternalAgentDelegate.is_available("claude") is False

    def test_is_available_installed(self):
        with patch("pocketclaw.agents.delegation.shutil.which", return_value="/usr/bin/claude"):
            assert ExternalAgentDelegate.is_available("claude") is True

    def test_unknown_agent(self):
        assert ExternalAgentDelegate.is_available("unknown") is False

    async def test_run_unknown_agent(self):
        result = await ExternalAgentDelegate.run("unknown", "test")
        assert result.exit_code == 1
        assert "Unknown agent" in result.error

    async def test_run_claude_not_installed(self):
        with patch("pocketclaw.agents.delegation.shutil.which", return_value=None):
            result = await ExternalAgentDelegate.run("claude", "test")
            assert result.exit_code == 1
            assert "not found" in result.error


# ---------------------------------------------------------------------------
# DelegateToClaudeCodeTool
# ---------------------------------------------------------------------------


class TestDelegateTool:
    def test_name(self):
        tool = DelegateToClaudeCodeTool()
        assert tool.name == "delegate_claude_code"

    def test_trust_level(self):
        tool = DelegateToClaudeCodeTool()
        assert tool.trust_level == "critical"

    def test_parameters(self):
        tool = DelegateToClaudeCodeTool()
        assert "task" in tool.parameters["properties"]
        assert "timeout" in tool.parameters["properties"]

    async def test_execute_not_installed(self):
        tool = DelegateToClaudeCodeTool()
        with patch(
            "pocketclaw.agents.delegation.ExternalAgentDelegate.is_available",
            return_value=False,
        ):
            result = await tool.execute(task="test task")
            assert "Error" in result
            assert "not found" in result.lower()

    async def test_execute_with_error(self):
        tool = DelegateToClaudeCodeTool()
        mock_result = DelegationResult(
            agent="claude", output="", exit_code=1, error="Process failed"
        )
        with (
            patch(
                "pocketclaw.agents.delegation.ExternalAgentDelegate.is_available",
                return_value=True,
            ),
            patch(
                "pocketclaw.agents.delegation.ExternalAgentDelegate.run",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
        ):
            result = await tool.execute(task="test task")
            assert "Error" in result
            assert "Process failed" in result

    async def test_execute_success(self):
        tool = DelegateToClaudeCodeTool()
        mock_result = DelegationResult(
            agent="claude", output="Task completed successfully", exit_code=0
        )
        with (
            patch(
                "pocketclaw.agents.delegation.ExternalAgentDelegate.is_available",
                return_value=True,
            ),
            patch(
                "pocketclaw.agents.delegation.ExternalAgentDelegate.run",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
        ):
            result = await tool.execute(task="test task")
            assert "Claude Code Result" in result
            assert "Task completed successfully" in result
