"""Smoke tests for the langchain_react backend.

The backend subclasses ``DeepAgentsBackend`` so the model build / tool
wiring / streaming logic is already covered by ``test_deep_agents_backend.py``.
These tests cover the parts that DIFFER:
  * info() metadata
  * registry entry
  * _initialize bypasses the deepagents import
  * _get_or_create_agent uses langgraph create_react_agent

Change (2026-06-10): added ``TestLangchainReactLocalModelLane`` to prove the
local / bring-your-own-model (Ollama) lane — the supported path for a
local-only self-hosted tenant. It asserts the LangChain model build routes to
``init_chat_model("ollama:<model>", base_url=<local host>)`` (no cloud key,
request stays on the box) and, when ``langchain-ollama`` is installed, that a
real ``ChatOllama`` is bound to the operator-set local host. It also pins the
cloud-egress trap as a regression guard: ``deep_agents_model`` defaults to
``anthropic:claude-sonnet-4-6`` and that prefix wins over ``llm_provider``, so a
local-only deployment MUST set ``deep_agents_model=ollama:<model>`` or traffic
silently goes to the Anthropic cloud (documented in
docs/deployment/self-hosting.mdx).
"""

from __future__ import annotations

import importlib.util
from unittest.mock import MagicMock, patch

from pocketpaw.agents.backend import Capability
from pocketpaw.agents.registry import get_backend_class
from pocketpaw.config import Settings

_HAS_LANGCHAIN_OLLAMA = importlib.util.find_spec("langchain_ollama") is not None


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


class TestLangchainReactLocalModelLane:
    """Prove the local / bring-your-own-model (Ollama) lane end-to-end at the
    model-build seam.

    The compliance ICP (e.g. a law firm: "local/BYO models only, data never
    leaves the box") runs ``agent_backend=langchain_react`` against a local
    Ollama server. The faithful seam is ``DeepAgentsBackend._build_model``,
    which ``LangchainReactBackend`` inherits: it imports
    ``langchain.chat_models.init_chat_model`` at call time and, for the Ollama
    provider, calls it with ``"ollama:<model>"`` and ``base_url=<ollama_host>``.
    We mock that transport seam and assert the request is wired to the local
    host in the right format, with NO cloud API key.
    """

    def _build_backend(self, **settings_kwargs):
        from pocketpaw.agents.langchain_react import LangchainReactBackend

        return LangchainReactBackend(Settings(agent_backend="langchain_react", **settings_kwargs))

    def test_local_model_routes_to_ollama_host_no_cloud_key(self):
        """deep_agents_model=ollama:<model> + ollama_host must build
        init_chat_model("ollama:<model>", base_url=<host>) with no api_key.

        The local host must be the operator-set value (a private GPU box),
        proving the request targets the local server, not the localhost
        default — and that no cloud credential is attached, so the request
        cannot leave the box.
        """
        backend = self._build_backend(
            llm_provider="ollama",
            deep_agents_model="ollama:llama3.2",
            ollama_host="http://ollama.internal:11434",
        )
        with patch("langchain.chat_models.init_chat_model", return_value=MagicMock()) as mock_init:
            backend._build_model()

        mock_init.assert_called_once()
        args, kwargs = mock_init.call_args
        # Format: provider-prefixed model id pointing at Ollama.
        assert args[0] == "ollama:llama3.2"
        # Transport: the operator's local host, NOT the localhost default.
        assert kwargs.get("base_url") == "http://ollama.internal:11434"
        # Egress guard: no cloud API key of any kind is passed.
        assert "api_key" not in kwargs
        assert "google_api_key" not in kwargs

    def test_local_model_default_host_when_unset(self):
        """With no ollama_host override the build still targets a local host
        (the 11434 default), never a cloud endpoint."""
        backend = self._build_backend(
            llm_provider="ollama",
            deep_agents_model="ollama:llama3.2",
        )
        with patch("langchain.chat_models.init_chat_model", return_value=MagicMock()) as mock_init:
            backend._build_model()

        _args, kwargs = mock_init.call_args
        assert kwargs.get("base_url") == "http://localhost:11434"
        assert "api_key" not in kwargs

    def test_real_chatollama_bound_to_local_host(self):
        """When langchain-ollama is installed, the UNMOCKED build must produce
        a real ChatOllama bound to the operator-set local host.

        This is the part a mock can't fake: it proves init_chat_model actually
        resolves the ``ollama:`` prefix to langchain_ollama.ChatOllama (the
        client that speaks Ollama's native /api/chat), and that our host config
        lands on it. If langchain-ollama is missing the lane is unsupported, so
        we assert the ImportError surfaces rather than silently passing.
        """
        backend = self._build_backend(
            llm_provider="ollama",
            deep_agents_model="ollama:llama3.2",
            ollama_host="http://ollama.internal:11434",
        )

        if not _HAS_LANGCHAIN_OLLAMA:
            import pytest

            # No silent green: without the binding the lane must fail loudly.
            with pytest.raises((ImportError, ValueError)):
                backend._build_model()
            return

        model = backend._build_model()
        from langchain_ollama import ChatOllama

        assert isinstance(model, ChatOllama)
        # The operator's local host is bound to the real client.
        assert getattr(model, "base_url", None) == "http://ollama.internal:11434"
        assert getattr(model, "model", None) == "llama3.2"

    def test_default_model_leaks_to_anthropic_cloud_regression_guard(self):
        """REGRESSION GUARD for the cloud-egress trap.

        ``deep_agents_model`` defaults to ``anthropic:claude-sonnet-4-6`` and
        that explicit provider prefix wins over ``llm_provider``. So an operator
        who sets ONLY ``llm_provider=ollama`` (leaving deep_agents_model at its
        default) gets routed to the Anthropic CLOUD — the exact data egress a
        local-only tenant must avoid. The supported config therefore REQUIRES
        ``deep_agents_model=ollama:<model>`` (see self-hosting docs).

        This test pins that behaviour: if a future change makes llm_provider
        win, it fails here and forces a deliberate review of the docs + the
        local-only contract rather than a silent semantics flip.
        """
        backend = self._build_backend(
            llm_provider="ollama",
            ollama_host="http://localhost:11434",
            # deep_agents_model intentionally left at its default.
        )
        with patch("langchain.chat_models.init_chat_model", return_value=MagicMock()) as mock_init:
            backend._build_model()

        args, kwargs = mock_init.call_args
        # Documents the trap: default prefix routes to Anthropic, not Ollama.
        assert args[0].startswith("anthropic:")
        # And crucially does NOT point at the local Ollama host.
        assert kwargs.get("base_url") != "http://localhost:11434"
