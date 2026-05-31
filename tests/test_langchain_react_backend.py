"""Smoke tests for the langchain_react backend.

The backend subclasses ``DeepAgentsBackend`` so the model build / tool
wiring / streaming logic is already covered by ``test_deep_agents_backend.py``.
These tests cover the parts that DIFFER:
  * info() metadata
  * registry entry
  * _initialize bypasses the deepagents import
  * _get_or_create_agent uses langgraph create_react_agent
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pocketpaw.agents.backend import Capability
from pocketpaw.agents.registry import get_backend_class
from pocketpaw.config import Settings


class TestLangchainReactInfo:
    def test_info_name(self):
        from pocketpaw.agents.langchain_react import LangchainReactBackend

        info = LangchainReactBackend.info()
        assert info.name == "langchain_react"

    def test_info_capabilities(self):
        from pocketpaw.agents.langchain_react import LangchainReactBackend

        info = LangchainReactBackend.info()
        assert Capability.STREAMING in info.capabilities
        assert Capability.TOOLS in info.capabilities
        assert Capability.MCP in info.capabilities
        assert Capability.MULTI_TURN in info.capabilities

    def test_info_no_builtin_tools(self):
        """The thin react backend ships no built-in tools — all tools
        come through MCP / custom-tool bridge. Distinguishes it from
        deep_agents which adds write_todos / task / fs tools."""
        from pocketpaw.agents.langchain_react import LangchainReactBackend

        info = LangchainReactBackend.info()
        assert info.builtin_tools == []


class TestLangchainReactRegistry:
    def test_registered(self):
        cls = get_backend_class("langchain_react")
        assert cls is not None
        from pocketpaw.agents.langchain_react import LangchainReactBackend

        assert cls is LangchainReactBackend


class TestLangchainReactInitialize:
    def test_does_not_require_deepagents_package(self):
        """The backend must initialize without `deepagents` installed —
        that's the entire point. Simulate ImportError on the deepagents
        path and confirm _sdk_available reflects langgraph instead."""
        # Block ``import deepagents`` to prove the subclass really
        # bypasses the parent's check.
        import sys

        from pocketpaw.agents.langchain_react import LangchainReactBackend

        original = sys.modules.get("deepagents")
        sys.modules["deepagents"] = None  # type: ignore[assignment]
        try:
            backend = LangchainReactBackend(Settings())
            # langgraph is part of the same install group so it'll be
            # importable; assert _sdk_available follows langgraph's
            # availability, not deepagents's.
            try:
                import langgraph.prebuilt  # noqa: F401

                expected = True
            except ImportError:
                expected = False
            assert backend._sdk_available is expected
        finally:
            if original is not None:
                sys.modules["deepagents"] = original
            else:
                sys.modules.pop("deepagents", None)


class TestLangchainReactAgentFactory:
    def test_get_or_create_agent_uses_create_react_agent(self):
        """The agent factory must call langgraph's create_react_agent,
        not deepagents.create_deep_agent."""
        from pocketpaw.agents.langchain_react import LangchainReactBackend

        backend = LangchainReactBackend(Settings(deep_agents_model="anthropic:claude-sonnet-4-6"))
        # Avoid going to the real tool bridge.
        backend._custom_tools = []

        fake_agent = MagicMock(name="compiled_graph")
        with patch(
            "langgraph.prebuilt.create_react_agent",
            return_value=fake_agent,
        ) as mock_factory:
            result = backend._get_or_create_agent(
                model=MagicMock(name="chat_model"),
                instructions="you are helpful",
                mcp_tools=[],
            )

        assert result is fake_agent
        mock_factory.assert_called_once()
        _args, kwargs = mock_factory.call_args
        # System prompt must reach the agent.
        assert kwargs.get("prompt") == "you are helpful"
        # Tool list must be passed (even if empty).
        assert "tools" in kwargs

    def test_agent_cache_reused_within_same_model_key(self):
        from pocketpaw.agents.langchain_react import LangchainReactBackend

        backend = LangchainReactBackend(Settings(deep_agents_model="anthropic:claude-sonnet-4-6"))
        backend._custom_tools = []

        fake_agent = MagicMock(name="compiled_graph")
        with patch(
            "langgraph.prebuilt.create_react_agent",
            return_value=fake_agent,
        ) as mock_factory:
            a1 = backend._get_or_create_agent(MagicMock(), "p", mcp_tools=[])
            a2 = backend._get_or_create_agent(MagicMock(), "p", mcp_tools=[])
        assert a1 is a2
        assert mock_factory.call_count == 1


class TestLangchainReactStatus:
    async def test_status_reports_correct_backend_name(self):
        from pocketpaw.agents.langchain_react import LangchainReactBackend

        backend = LangchainReactBackend(Settings(deep_agents_model="anthropic:claude-sonnet-4-6"))
        status = await backend.get_status()
        assert status["backend"] == "langchain_react"
