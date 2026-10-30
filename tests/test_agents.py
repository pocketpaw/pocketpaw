"""
Tests for Agent backends, protocol, and router.

Updated for multi-SDK architecture:
- ClaudeSDKBackend (was ClaudeAgentSDKWrapper)
- Registry-based routing
- Removed: ExecutorProtocol, OrchestratorProtocol, pocketpaw_native, open_interpreter
"""

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

    @pytest.mark.asyncio
    async def test_dangerous_hook_fails_closed_on_exception(self):
        """Hook must block (not allow) commands when an internal error occurs.

        Regression test for GH-852: a RuntimeError inside the hook previously
        returned {} (allow). After the fix it must return a deny decision.
        """
        from unittest.mock import patch

        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        sdk = ClaudeSDKBackend(Settings())

        input_data = {"tool_name": "Bash", "tool_input": {"command": "ls"}}

        # Inject a RuntimeError into _is_dangerous_command so the outer
        # except clause fires.
        with patch.object(sdk, "_is_dangerous_command", side_effect=RuntimeError("boom")):
            result = await sdk._block_dangerous_hook(input_data, None, None)

        # Must deny, not allow
        hook_output = result.get("hookSpecificOutput", {})
        assert hook_output.get("permissionDecision") == "deny"
        assert "internal error" in hook_output.get("permissionDecisionReason", "").lower()

    def test_sdk_has_system_prompt(self):
        from pocketpaw.agents.claude_sdk import _DEFAULT_IDENTITY

        assert "PocketPaw" in _DEFAULT_IDENTITY

    def test_default_instructions_exist(self):
        from pocketpaw.bootstrap.default_provider import _DEFAULT_INSTRUCTIONS

        assert len(_DEFAULT_INSTRUCTIONS) > 100
        assert "PocketPaw Tools" in _DEFAULT_INSTRUCTIONS

    def test_sdk_has_dangerous_patterns(self):
        from pocketpaw.security.rails import DANGEROUS_SUBSTRINGS

        assert isinstance(DANGEROUS_SUBSTRINGS, list)
        assert "rm -rf /" in DANGEROUS_SUBSTRINGS

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

    def test_router_defaults_to_claude_agent_sdk(self, monkeypatch):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
        from pocketpaw.agents.router import AgentRouter

        monkeypatch.delenv("POCKETPAW_AGENT_BACKEND", raising=False)
        settings = Settings(_env_file=None)
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

    def test_router_get_backend_info(self, monkeypatch):
        from pocketpaw.agents.router import AgentRouter

        monkeypatch.delenv("POCKETPAW_AGENT_BACKEND", raising=False)
        router = AgentRouter(Settings(_env_file=None))
        info = router.get_backend_info()
        assert info is not None
        assert info.name == "claude_agent_sdk"

    def test_router_get_backend_info_uses_fallback_backend(self):
        """When configured backend is invalid, info should reflect active fallback backend."""
        from pocketpaw.agents.router import AgentRouter

        router = AgentRouter(Settings(agent_backend="unknown_backend_xyz"))
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

    def test_auto_resolve_no_key_gives_ollama(self, monkeypatch):
        from pocketpaw.llm.client import resolve_llm_client

        monkeypatch.delenv("POCKETPAW_LLM_PROVIDER", raising=False)
        settings = Settings(_env_file=None)
        llm = resolve_llm_client(settings)
        assert llm.provider == "ollama"

    def test_force_anthropic_no_key_returns_anthropic(self, monkeypatch):
        from pocketpaw.llm.client import resolve_llm_client

        monkeypatch.delenv("POCKETPAW_LLM_PROVIDER", raising=False)
        settings = Settings(_env_file=None)
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
    async def test_claude_sdk_run_resolves_anthropic_not_ollama(self, monkeypatch):
        """run() should resolve to anthropic, not fall to ollama."""
        from unittest.mock import patch

        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
        from pocketpaw.llm.client import resolve_llm_client as real_resolve

        monkeypatch.delenv("POCKETPAW_LLM_PROVIDER", raising=False)
        settings = Settings(
            _env_file=None, agent_backend="claude_agent_sdk", smart_routing_enabled=False
        )
        with patch("shutil.which", return_value="/usr/bin/claude"):
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


# =============================================================================
# ART-2: per-run cwd resolution + fail-closed (agent_extensions seam)
# =============================================================================


class TestResolveCwd:
    """``ClaudeSDKBackend._resolve_cwd`` — the OSS/cloud cwd gate.

    OSS (no ``agent_extensions`` provider) must stay on ``file_jail_path``; an
    EE provider's ``agent_cwd`` wins; a provider that RAISES (a cloud run with
    no resolvable workspace) propagates — fail-closed, never a silent fallback.
    """

    def test_oss_defaults_to_file_jail_path(self, monkeypatch):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        monkeypatch.setattr("pocketpaw._registry.providers", lambda group: ())
        settings = Settings()
        backend = ClaudeSDKBackend(settings)
        assert backend._resolve_cwd() == settings.file_jail_path

    def test_extension_cwd_wins(self, monkeypatch, tmp_path):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        jail = tmp_path / "jail" / "wsX"

        class FakeExt:
            def agent_cwd(self):
                return str(jail)

        monkeypatch.setattr("pocketpaw._registry.providers", lambda group: (FakeExt(),))
        backend = ClaudeSDKBackend(Settings())
        assert backend._resolve_cwd() == jail

    def test_extension_raise_propagates_fail_closed(self, monkeypatch):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        class FailClosedExt:
            def agent_cwd(self):
                raise RuntimeError("no resolvable workspace_id")

        monkeypatch.setattr("pocketpaw._registry.providers", lambda group: (FailClosedExt(),))
        backend = ClaudeSDKBackend(Settings())
        with pytest.raises(RuntimeError, match="no resolvable workspace_id"):
            backend._resolve_cwd()

    def test_extension_returning_none_falls_back(self, monkeypatch):
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        class NoneExt:
            def agent_cwd(self):
                return None

        monkeypatch.setattr("pocketpaw._registry.providers", lambda group: (NoneExt(),))
        settings = Settings()
        backend = ClaudeSDKBackend(settings)
        assert backend._resolve_cwd() == settings.file_jail_path

    def test_provider_without_agent_cwd_ignored(self, monkeypatch):
        # A provider predating agent_cwd must not break resolution.
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        class OldExt:
            def subprocess_env(self):
                return {}

        monkeypatch.setattr("pocketpaw._registry.providers", lambda group: (OldExt(),))
        settings = Settings()
        backend = ClaudeSDKBackend(settings)
        assert backend._resolve_cwd() == settings.file_jail_path


class TestCwdJailHardening:
    """ART-2 hardening: run()-level fail-closed lock + cwd in the cache key."""

    @pytest.mark.asyncio
    async def test_run_fails_closed_no_home_fallback(self, monkeypatch):
        """A cloud run whose cwd resolver RAISES must surface an error event and
        NEVER execute the agent with a file_jail_path / home fallback cwd.

        Locks the fail-closed at the run() seam, not just the _resolve_cwd unit:
        guards against a future edit adding a generic cwd fallback in run()'s
        except handler (the known cloud-fail-closed-slips-past-green-OSS-CI
        pattern). Asserts the CLOUD branch — the resolver raises.

        The OSS run() seam knows ONLY that the extension's ``agent_cwd`` raised;
        it does not see the EE marker. In production (fix/cloud-artifacts-reland)
        the EE resolver raises exactly when a cloud CHAT run — the
        ``mark_cloud_chat_run`` dispatch — loses its workspace; the marker-gated
        side of that contract is covered in tests/cloud/agents/test_agent_jail.py.
        This mock stands in for that fail-closed without coupling the OSS test to
        EE internals.
        """
        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        settings = Settings()
        backend = ClaudeSDKBackend(settings)
        backend._sdk_available = True
        backend._cli_available = True

        class FailClosedExt:
            # Stands in for the EE resolver fail-closing on a marker-set cloud
            # CHAT run that lost its workspace (the real mis-tenanting signal).
            def agent_cwd(self):
                raise RuntimeError("no resolvable workspace_id")

        monkeypatch.setattr("pocketpaw._registry.providers", lambda group: (FailClosedExt(),))

        # Capture the cwd of every SDK-options construction — the only gateway
        # to actually launching the agent. On the fail-closed path the resolver
        # raises first, so options are never built at all.
        built_cwds: list[str | None] = []

        def spy_options(**kwargs):
            built_cwds.append(kwargs.get("cwd"))
            raise RuntimeError("options must not be built on the fail-closed path")

        backend._ClaudeAgentOptions = spy_options

        events = [e async for e in backend.run("hi", session_key="s1")]

        # (1) the run surfaces an error event
        assert any(e.type == "error" for e in events)
        # (2) the agent NEVER executed with a home / file_jail_path fallback cwd
        assert str(settings.file_jail_path) not in built_cwds
        # in fact nothing was built — the raise precedes options construction
        assert built_cwds == []

    def test_cache_key_includes_cwd(self):
        """Two different resolved cwds → different cache keys (no warm-client
        reuse across tenants); identical inputs → identical key (reuse kept)."""
        from types import SimpleNamespace

        from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

        def opts(cwd):
            return SimpleNamespace(
                cwd=cwd, model="claude-x", allowed_tools=["Read"], system_prompt="p"
            )

        key_a = ClaudeSDKBackend._client_cache_key(opts("/jail/wsA/agent/s1"), session_key="s1")
        key_b = ClaudeSDKBackend._client_cache_key(opts("/jail/wsB/agent/s1"), session_key="s1")
        key_a2 = ClaudeSDKBackend._client_cache_key(opts("/jail/wsA/agent/s1"), session_key="s1")
        assert key_a != key_b
        assert key_a == key_a2
