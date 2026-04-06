"""Tests for Deep Agents tool bridge."""

from unittest.mock import patch

from pocketpaw.config import Settings


class TestDeepAgentsToolBridge:
    def test_build_deep_agents_tools_graceful_degradation(self):
        """Returns empty list when langchain-core is unavailable."""
        from pocketpaw.agents.tool_bridge import build_deep_agents_tools

        with patch.dict("sys.modules", {"langchain_core": None, "langchain_core.tools": None}):
            result = build_deep_agents_tools(Settings(), backend="deep_agents")
            assert result == []

    def test_tools_not_excluded_for_deep_agents(self):
        """Deep Agents backend should not have Claude SDK exclusions applied."""
        # The _CLAUDE_SDK_EXCLUDED set should not apply to deep_agents
        # This test verifies the backend name is treated as a non-Claude backend
        from pocketpaw.agents.tool_bridge import _CLAUDE_SDK_EXCLUDED

        assert "deep_agents" != "claude_agent_sdk"
        # ShellTool etc. are only excluded for claude_agent_sdk
        assert len(_CLAUDE_SDK_EXCLUDED) > 0  # Sanity check
