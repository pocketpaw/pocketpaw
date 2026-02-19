"""
Tests for Agent backends, protocol, and router.

Updated for multi-SDK architecture:
- ClaudeSDKBackend (was ClaudeAgentSDKWrapper)
- Registry-based routing
- Removed: ExecutorProtocol, OrchestratorProtocol, pocketpaw_native, open_interpreter
"""

from pathlib import Path

import pytest

from pocketpaw.config import Settings

# =============================================================================
# PROTOCOL TESTS
# =============================================================================


class TestAgentProtocol:
    """Tests for the agent protocol module."""

    def test_agent_event_creation(self):
        from pocketpaw.agents.protocol import AgentEvent

        event = AgentEvent(type="message", content="Hello")
        assert event.type == "message"
        assert event.content == "Hello"
        assert event.metadata == {}

    def test_agent_event_with_metadata(self):
        from pocketpaw.agents.protocol import AgentEvent

        event = AgentEvent(type="tool_use", content="Using Bash", metadata={"name": "Bash"})
        assert event.metadata == {"name": "Bash"}

    def test_agent_event_types(self):
        from pocketpaw.agents.protocol import AgentEvent

        types = ["message", "tool_use", "tool_result", "thinking", "error", "done"]
        for event_type in types:
            event = AgentEvent(type=event_type, content="test")
            assert event.type == event_type


# =============================================================================
# CLAUDE AGENT SDK TESTS
# =============================================================================


class TestClaudeAgentSDK:
    """Tests for Claude Agent SDK backend."""

    def test_sdk_class_importable(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        assert ClaudeSDKBackend is not None

    def test_backward_compat_aliases(self):
        from pocketpaw.agents.claude_sdk import (
            ClaudeAgentSDK,
            ClaudeAgentSDKWrapper,
            ClaudeSDKBackend,
        )

        assert ClaudeAgentSDK is ClaudeSDKBackend
        assert ClaudeAgentSDKWrapper is ClaudeSDKBackend

    def test_sdk_initializes_without_error(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        settings = Settings()
        backend = ClaudeSDKBackend(settings)
        assert backend is not None

    def test_info_static_method(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        info = ClaudeSDKBackend.info()
        assert info.name == "claude_agent_sdk"
        assert info.display_name == "Claude Agent SDK"
        assert "Bash" in info.builtin_tools
        assert info.tool_policy_map["Bash"] == "shell"

    def test_dangerous_pattern_detection(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        settings = Settings()
        sdk = ClaudeSDKBackend(settings)

        assert sdk._is_dangerous_command("rm -rf /") is not None
        assert sdk._is_dangerous_command("rm -rf ~") is not None
        assert sdk._is_dangerous_command("sudo rm /important") is not None
        assert sdk._is_dangerous_command("ls -la") is None
        assert sdk._is_dangerous_command("cat file.txt") is None

    def test_sdk_has_system_prompt(self):
        from pocketpaw.agents.claude_sdk import _DEFAULT_IDENTITY

        assert "PocketPaw" in _DEFAULT_IDENTITY

    def test_default_instructions_exist(self):
        from pocketpaw.bootstrap.default_provider import _DEFAULT_INSTRUCTIONS

        assert len(_DEFAULT_INSTRUCTIONS) > 100
        assert "PocketPaw Tools" in _DEFAULT_INSTRUCTIONS

    def test_sdk_has_dangerous_patterns(self):
        from pocketpaw.agents.claude_sdk import DANGEROUS_PATTERNS

        assert isinstance(DANGEROUS_PATTERNS, list)
        assert "rm -rf /" in DANGEROUS_PATTERNS

    @pytest.mark.asyncio
    async def test_sdk_status(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        sdk = ClaudeSDKBackend(Settings())
        status = await sdk.get_status()
        assert status["backend"] == "claude_agent_sdk"
        assert "available" in status

    @pytest.mark.asyncio
    async def test_sdk_stop(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        sdk = ClaudeSDKBackend(Settings())
        assert sdk._stop_flag is False
        await sdk.stop()
        assert sdk._stop_flag is True

    def test_sdk_set_working_directory(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        sdk = ClaudeSDKBackend(Settings())
        new_path = Path("/tmp")
        sdk.set_working_directory(new_path)
        assert sdk._cwd == new_path

    @pytest.mark.asyncio
    async def test_sdk_run_without_sdk_installed(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        sdk = ClaudeSDKBackend(Settings())
        sdk._sdk_available = False

        events = []
        async for event in sdk.run("test message"):
            events.append(event)

        assert len(events) == 1
        assert events[0].type == "error"
        assert "not found" in events[0].content.lower()


# =============================================================================
# ROUTER TESTS
# =============================================================================


class TestAgentRouter:
    """Tests for agent router (registry-based)."""

    def test_router_importable(self):
        from pocketpaw.agents.router import AgentRouter

        assert AgentRouter is not None

    def test_router_defaults_to_claude_agent_sdk(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
        from pocketpaw.agents.router import AgentRouter

        settings = Settings()
        router = AgentRouter(settings)
        assert router._backend is not None
        assert isinstance(router._backend, ClaudeSDKBackend)

    def test_router_selects_claude_agent_sdk(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
        from pocketpaw.agents.router import AgentRouter

        settings = Settings(agent_backend="claude_agent_sdk")
        router = AgentRouter(settings)
        assert isinstance(router._backend, ClaudeSDKBackend)

    def test_router_legacy_pocketpaw_native_falls_back(self):
        """Legacy 'pocketpaw_native' should fall back to claude_agent_sdk."""
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
        from pocketpaw.agents.router import AgentRouter

        settings = Settings(agent_backend="pocketpaw_native")
        router = AgentRouter(settings)
        assert isinstance(router._backend, ClaudeSDKBackend)

    def test_router_legacy_open_interpreter_falls_back(self):
        """Legacy 'open_interpreter' should fall back to claude_agent_sdk."""
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
        from pocketpaw.agents.router import AgentRouter

        settings = Settings(agent_backend="open_interpreter")
        router = AgentRouter(settings)
        assert isinstance(router._backend, ClaudeSDKBackend)

    def test_router_falls_back_on_unknown(self):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
        from pocketpaw.agents.router import AgentRouter

        settings = Settings(agent_backend="unknown_backend_xyz")
        router = AgentRouter(settings)
        assert isinstance(router._backend, ClaudeSDKBackend)

    def test_router_get_backend_info(self):
        from pocketpaw.agents.router import AgentRouter

        router = AgentRouter(Settings())
        info = router.get_backend_info()
        assert info is not None
        assert info.name == "claude_agent_sdk"

    @pytest.mark.asyncio
    async def test_router_has_run_method(self):
        from pocketpaw.agents.router import AgentRouter

        router = AgentRouter(Settings())
        assert hasattr(router, "run")

    @pytest.mark.asyncio
    async def test_router_has_stop_method(self):
        from pocketpaw.agents.router import AgentRouter

        router = AgentRouter(Settings())
        assert hasattr(router, "stop")
        await router.stop()


# =============================================================================
# CLAUDE SDK CLI AUTH BUG — REPRODUCTION TESTS
# =============================================================================


class TestClaudeSDKCliAuth:
    """Bug reproduction: PocketPaw requires API key even when Claude CLI is authenticated."""

    def test_auto_resolve_no_key_gives_ollama(self):
        from pocketpaw.llm.client import resolve_llm_client

        settings = Settings()
        llm = resolve_llm_client(settings)
        assert llm.provider == "ollama"

    def test_force_anthropic_no_key_returns_anthropic(self):
        from pocketpaw.llm.client import resolve_llm_client

        settings = Settings()
        llm = resolve_llm_client(settings, force_provider="anthropic")
        assert llm.provider == "anthropic"
        assert llm.api_key is None

    def test_no_key_anthropic_to_sdk_env_is_empty(self):
        from pocketpaw.llm.client import LLMClient

        llm = LLMClient(
            provider="anthropic",
            model="claude-sonnet-4-5-20250929",
            api_key=None,
            ollama_host="http://localhost:11434",
        )
        assert llm.to_sdk_env() == {}

    @pytest.mark.asyncio
    async def test_claude_sdk_run_resolves_anthropic_not_ollama(self):
        """run() should resolve to anthropic, not fall to ollama."""
        from unittest.mock import patch

        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
        from pocketpaw.llm.client import resolve_llm_client as real_resolve

        settings = Settings(agent_backend="claude_agent_sdk", smart_routing_enabled=False)
        sdk = ClaudeSDKBackend(settings)

        resolved_providers: list[str] = []

        def spy_resolve(s, **kwargs):
            result = real_resolve(s, **kwargs)
            resolved_providers.append(result.provider)
            return result

        def stop_execution(**kwargs):
            raise RuntimeError("test_stop_before_sdk_query")

        with patch("pocketpaw.llm.client.resolve_llm_client", side_effect=spy_resolve):
            sdk._ClaudeAgentOptions = stop_execution
            events = []
            async for event in sdk.run("test"):
                events.append(event)

        assert len(resolved_providers) > 0
        assert resolved_providers[0] == "anthropic"
