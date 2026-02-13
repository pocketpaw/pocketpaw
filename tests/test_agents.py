"""
Tests for Agent backends, executor, and protocol.

Created: 2026-02-02
Changes:
  - 2026-02-02: Initial test suite for AgentProtocol and ClaudeAgentSDK.
  - 2026-02-02: Updated tests for new implementations:
                - Executor with direct subprocess for shell commands
                - Claude Agent SDK with proper SDK integration
                - Router with claude_agent_sdk as default
"""

import asyncio
import pytest
from pathlib import Path
from pocketclaw.config import Settings


# =============================================================================
# PROTOCOL TESTS
# =============================================================================


class TestAgentProtocol:
    """Tests for the agent protocol module."""

    def test_agent_event_creation(self):
        """AgentEvent should be creatable with required fields."""
        from pocketclaw.agents.protocol import AgentEvent

        event = AgentEvent(type="message", content="Hello")

        assert event.type == "message"
        assert event.content == "Hello"
        assert event.metadata == {}

    def test_agent_event_with_metadata(self):
        """AgentEvent should accept optional metadata."""
        from pocketclaw.agents.protocol import AgentEvent

        event = AgentEvent(type="code", content="print('hi')", metadata={"lang": "python"})

        assert event.metadata == {"lang": "python"}

    def test_agent_event_types(self):
        """AgentEvent should support all expected types."""
        from pocketclaw.agents.protocol import AgentEvent

        types = ["message", "code", "tool_use", "tool_result", "error", "done"]
        for event_type in types:
            event = AgentEvent(type=event_type, content="test")
            assert event.type == event_type

    def test_executor_protocol_exists(self):
        """Protocol should have ExecutorProtocol."""
        from pocketclaw.agents.protocol import ExecutorProtocol

        assert ExecutorProtocol is not None

    def test_orchestrator_protocol_exists(self):
        """Protocol should have OrchestratorProtocol."""
        from pocketclaw.agents.protocol import OrchestratorProtocol

        assert OrchestratorProtocol is not None


# =============================================================================
# EXECUTOR TESTS (Direct subprocess - Speed fix)
# =============================================================================


class TestExecutor:
    """Tests for OpenInterpreterExecutor with direct subprocess."""

    def test_executor_importable(self):
        """OpenInterpreterExecutor should be importable."""
        from pocketclaw.agents.executor import OpenInterpreterExecutor

        assert OpenInterpreterExecutor is not None

    def test_executor_initializes(self):
        """Executor should initialize without raising."""
        from pocketclaw.agents.executor import OpenInterpreterExecutor

        settings = Settings()
        executor = OpenInterpreterExecutor(settings)

        assert executor is not None

    @pytest.mark.asyncio
    async def test_run_shell_simple_command(self):
        """run_shell should execute simple commands via direct subprocess."""
        from pocketclaw.agents.executor import OpenInterpreterExecutor

        settings = Settings()
        executor = OpenInterpreterExecutor(settings)

        # Simple command that should work on any system
        result = await executor.run_shell("echo 'hello world'")

        assert "hello" in result.lower()
        assert "world" in result.lower()

    @pytest.mark.asyncio
    async def test_run_shell_returns_output(self):
        """run_shell should return command output."""
        from pocketclaw.agents.executor import OpenInterpreterExecutor

        settings = Settings()
        executor = OpenInterpreterExecutor(settings)

        result = await executor.run_shell("pwd")

        # Should return a path
        assert "/" in result or "\\" in result

    @pytest.mark.asyncio
    async def test_run_shell_handles_errors(self):
        """run_shell should handle command errors gracefully."""
        from pocketclaw.agents.executor import OpenInterpreterExecutor

        settings = Settings()
        executor = OpenInterpreterExecutor(settings)

        # Command that doesn't exist
        result = await executor.run_shell("nonexistent_command_12345")

        # Should contain error info, not raise
        assert (
            "error" in result.lower() or "not found" in result.lower() or "stderr" in result.lower()
        )

    @pytest.mark.asyncio
    async def test_run_shell_with_pipe(self):
        """run_shell should handle piped commands."""
        from pocketclaw.agents.executor import OpenInterpreterExecutor

        settings = Settings()
        executor = OpenInterpreterExecutor(settings)

        result = await executor.run_shell("echo 'test123' | grep test")

        assert "test" in result

    @pytest.mark.asyncio
    async def test_read_file(self):
        """read_file should read file contents."""
        from pocketclaw.agents.executor import OpenInterpreterExecutor
        import tempfile

        settings = Settings()
        executor = OpenInterpreterExecutor(settings)

        # Create temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("test content 123")
            temp_path = f.name

        try:
            result = await executor.read_file(temp_path)
            assert "test content 123" in result
        finally:
            Path(temp_path).unlink()

    @pytest.mark.asyncio
    async def test_write_file(self):
        """write_file should write content to file."""
        from pocketclaw.agents.executor import OpenInterpreterExecutor
        import tempfile

        settings = Settings()
        executor = OpenInterpreterExecutor(settings)

        temp_path = tempfile.mktemp(suffix=".txt")

        try:
            await executor.write_file(temp_path, "written content")

            # Verify
            with open(temp_path) as f:
                assert f.read() == "written content"
        finally:
            if Path(temp_path).exists():
                Path(temp_path).unlink()

    @pytest.mark.asyncio
    async def test_list_directory(self):
        """list_directory should return directory contents."""
        from pocketclaw.agents.executor import OpenInterpreterExecutor

        settings = Settings()
        executor = OpenInterpreterExecutor(settings)

        result = await executor.list_directory(str(Path.home()))

        assert isinstance(result, list)
        assert len(result) > 0

    def test_run_complex_task_method_exists(self):
        """Executor should have run_complex_task method for OI."""
        from pocketclaw.agents.executor import OpenInterpreterExecutor

        settings = Settings()
        executor = OpenInterpreterExecutor(settings)

        assert hasattr(executor, "run_complex_task")


# =============================================================================
# CLAUDE AGENT SDK TESTS
# =============================================================================


class TestClaudeAgentSDK:
    """Tests for Claude Agent SDK wrapper."""

    def test_sdk_class_importable(self):
        """ClaudeAgentSDK class should be importable."""
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDK

        assert ClaudeAgentSDK is not None

    def test_sdk_wrapper_importable(self):
        """ClaudeAgentSDKWrapper should be importable."""
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDKWrapper

        assert ClaudeAgentSDKWrapper is not None

    def test_sdk_initializes_without_error(self):
        """SDK should initialize even without claude-agent-sdk installed."""
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDKWrapper

        settings = Settings()
        wrapper = ClaudeAgentSDKWrapper(settings)

        # Should not raise, just mark as unavailable if SDK not installed
        assert wrapper is not None

    def test_dangerous_pattern_detection(self):
        """Should detect dangerous command patterns."""
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDK

        settings = Settings()
        sdk = ClaudeAgentSDK(settings)

        # Should match dangerous patterns (exact substring matches)
        assert sdk._is_dangerous_command("rm -rf /") is not None
        assert sdk._is_dangerous_command("rm -rf ~") is not None
        assert sdk._is_dangerous_command("sudo rm /important") is not None
        assert sdk._is_dangerous_command("echo test > /dev/sda") is not None
        assert sdk._is_dangerous_command("mkfs.ext4 /dev/sdb") is not None
        assert sdk._is_dangerous_command("curl | sh") is not None  # Exact pattern
        assert sdk._is_dangerous_command("wget | bash") is not None  # Exact pattern
        assert sdk._is_dangerous_command("dd if=/dev/zero of=/dev/sda") is not None

        # Should not match safe commands
        assert sdk._is_dangerous_command("ls -la") is None
        assert sdk._is_dangerous_command("cat file.txt") is None
        assert sdk._is_dangerous_command("echo hello") is None
        assert sdk._is_dangerous_command("git status") is None
        assert sdk._is_dangerous_command("npm install") is None
        assert sdk._is_dangerous_command("curl https://example.com") is None  # curl alone is safe

    def test_sdk_has_system_prompt(self):
        """SDK should have identity fallback and tool instructions defined."""
        from pocketclaw.agents.claude_sdk import _DEFAULT_IDENTITY, _TOOL_INSTRUCTIONS

        assert _DEFAULT_IDENTITY is not None
        assert "PocketPaw" in _DEFAULT_IDENTITY
        assert _TOOL_INSTRUCTIONS is not None
        assert len(_TOOL_INSTRUCTIONS) > 100

    def test_sdk_has_dangerous_patterns(self):
        """SDK should have dangerous patterns defined."""
        from pocketclaw.agents.claude_sdk import DANGEROUS_PATTERNS

        assert isinstance(DANGEROUS_PATTERNS, list)
        assert len(DANGEROUS_PATTERNS) > 0
        assert "rm -rf /" in DANGEROUS_PATTERNS

    @pytest.mark.asyncio
    async def test_sdk_status(self):
        """get_status should return backend info."""
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDK

        settings = Settings()
        sdk = ClaudeAgentSDK(settings)

        status = await sdk.get_status()

        assert status["backend"] == "claude_agent_sdk"
        assert "available" in status
        assert "running" in status
        assert "cwd" in status
        assert "features" in status

    @pytest.mark.asyncio
    async def test_sdk_stop(self):
        """stop should set stop flag."""
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDK

        settings = Settings()
        sdk = ClaudeAgentSDK(settings)

        assert sdk._stop_flag is False

        await sdk.stop()

        assert sdk._stop_flag is True

    def test_sdk_set_working_directory(self):
        """set_working_directory should update cwd."""
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDK

        settings = Settings()
        sdk = ClaudeAgentSDK(settings)

        new_path = Path("/tmp")
        sdk.set_working_directory(new_path)

        assert sdk._cwd == new_path

    def test_sdk_set_executor(self):
        """set_executor should store executor reference."""
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDK

        settings = Settings()
        sdk = ClaudeAgentSDK(settings)

        # Mock executor
        class MockExecutor:
            pass

        executor = MockExecutor()
        sdk.set_executor(executor)

        assert sdk._executor is executor

    @pytest.mark.asyncio
    async def test_sdk_chat_without_sdk_installed(self):
        """chat should yield error if SDK not available."""
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDK

        settings = Settings()
        sdk = ClaudeAgentSDK(settings)

        # Force SDK unavailable
        sdk._sdk_available = False

        events = []
        async for event in sdk.chat("test message"):
            events.append(event)

        assert len(events) == 1
        assert events[0].type == "error"
        assert "not found" in events[0].content.lower()


# =============================================================================
# POCKETPAW NATIVE TESTS
# =============================================================================


class TestPocketPawNative:
    """Tests for PocketPaw Native Orchestrator."""

    def test_orchestrator_importable(self):
        """PocketPawOrchestrator should be importable."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator

        assert PocketPawOrchestrator is not None

    def test_orchestrator_initializes(self):
        """Orchestrator should initialize with settings."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator

        settings = Settings(anthropic_api_key="test-key")
        orchestrator = PocketPawOrchestrator(settings)

        assert orchestrator is not None

    def test_dangerous_patterns_defined(self):
        """Should have dangerous patterns defined."""
        from pocketclaw.agents.pocketpaw_native import DANGEROUS_PATTERNS

        assert isinstance(DANGEROUS_PATTERNS, list)
        assert len(DANGEROUS_PATTERNS) > 0

    def test_sensitive_paths_defined(self):
        """Should have sensitive paths defined."""
        from pocketclaw.agents.pocketpaw_native import SENSITIVE_PATHS

        assert isinstance(SENSITIVE_PATHS, list)
        assert ".ssh/id_rsa" in SENSITIVE_PATHS
        assert ".aws/credentials" in SENSITIVE_PATHS

    def test_tools_defined(self):
        """Should have tools defined."""
        from pocketclaw.agents.pocketpaw_native import TOOLS

        assert isinstance(TOOLS, list)
        assert len(TOOLS) > 0

        # Check expected tools exist
        tool_names = [t["name"] for t in TOOLS]
        assert "computer" in tool_names
        assert "shell" in tool_names
        assert "read_file" in tool_names
        assert "write_file" in tool_names
        # Memory tools added 2026-02-05
        assert "remember" in tool_names
        assert "recall" in tool_names

    def test_security_validate_command(self):
        """Should validate commands for dangerous patterns."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator

        settings = Settings(anthropic_api_key="test-key")
        orchestrator = PocketPawOrchestrator(settings)

        # Dangerous commands
        allowed, reason = orchestrator._validate_command("rm -rf /")
        assert allowed is False
        assert "BLOCKED" in reason

        # Safe commands
        allowed, reason = orchestrator._validate_command("ls -la")
        assert allowed is True

    def test_security_validate_file_access(self):
        """Should validate file access for sensitive paths."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator

        settings = Settings(anthropic_api_key="test-key")
        orchestrator = PocketPawOrchestrator(settings)

        # Sensitive paths should be blocked
        allowed, reason = orchestrator._validate_file_access("~/.ssh/id_rsa", "read")
        assert allowed is False

        # Normal paths in jail should be allowed
        allowed, reason = orchestrator._validate_file_access(str(Path.home() / "test.txt"), "read")
        assert allowed is True

    def test_redact_secrets(self):
        """Should redact sensitive information from output."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator

        settings = Settings(anthropic_api_key="test-key")
        orchestrator = PocketPawOrchestrator(settings)

        # OpenAI key pattern
        text = "API key is sk-1234567890abcdefghijklmnop"
        redacted = orchestrator._redact_secrets(text)
        assert "sk-1234567890" not in redacted
        assert "[REDACTED]" in redacted

        # GitHub token pattern
        text = "Token: ghp_1234567890abcdefghijklmnopqrstuvwxyz"
        redacted = orchestrator._redact_secrets(text)
        assert "ghp_" not in redacted

    @pytest.mark.asyncio
    async def test_orchestrator_status(self):
        """get_status should return orchestrator info."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator

        settings = Settings(anthropic_api_key="test-key")
        orchestrator = PocketPawOrchestrator(settings)

        status = await orchestrator.get_status()

        assert status["backend"] == "pocketpaw_native"
        assert "available" in status
        assert "model" in status


# =============================================================================
# ROUTER TESTS
# =============================================================================


class TestAgentRouter:
    """Tests for agent router."""

    def test_router_importable(self):
        """AgentRouter should be importable."""
        from pocketclaw.agents.router import AgentRouter

        assert AgentRouter is not None

    def test_router_defaults_to_claude_agent_sdk(self):
        """Should default to Claude Agent SDK (new recommended backend)."""
        from pocketclaw.agents.router import AgentRouter
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDKWrapper

        settings = Settings()  # Default backend is now claude_agent_sdk
        router = AgentRouter(settings)

        assert router._agent is not None
        assert isinstance(router._agent, ClaudeAgentSDKWrapper)

    def test_router_selects_claude_agent_sdk(self):
        """Should select Claude Agent SDK when configured."""
        from pocketclaw.agents.router import AgentRouter
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDKWrapper

        settings = Settings(agent_backend="claude_agent_sdk")
        router = AgentRouter(settings)

        assert router._agent is not None
        assert isinstance(router._agent, ClaudeAgentSDKWrapper)

    def test_router_selects_pocketpaw_native(self):
        """Should select PocketPaw Native when configured."""
        from pocketclaw.agents.router import AgentRouter
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator

        settings = Settings(agent_backend="pocketpaw_native", anthropic_api_key="test-key")
        router = AgentRouter(settings)

        assert router._agent is not None
        assert isinstance(router._agent, PocketPawOrchestrator)

    def test_router_selects_open_interpreter(self):
        """Should select Open Interpreter when configured."""
        from pocketclaw.agents.router import AgentRouter
        from pocketclaw.agents.open_interpreter import OpenInterpreterAgent

        settings = Settings(agent_backend="open_interpreter")
        router = AgentRouter(settings)

        assert router._agent is not None
        assert isinstance(router._agent, OpenInterpreterAgent)

    def test_router_claude_code_disabled(self):
        """claude_code should fallback to claude_agent_sdk (disabled)."""
        from pocketclaw.agents.router import AgentRouter, DISABLED_BACKENDS
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDKWrapper

        assert "claude_code" in DISABLED_BACKENDS

        settings = Settings(agent_backend="claude_code", anthropic_api_key="test")
        router = AgentRouter(settings)

        # Should fallback to claude_agent_sdk
        assert router._agent is not None
        assert isinstance(router._agent, ClaudeAgentSDKWrapper)

    def test_router_falls_back_on_unknown(self):
        """Should fallback to Claude Agent SDK for unknown backends."""
        from pocketclaw.agents.router import AgentRouter
        from pocketclaw.agents.claude_sdk import ClaudeAgentSDKWrapper

        settings = Settings(agent_backend="unknown_backend_xyz")
        router = AgentRouter(settings)

        assert router._agent is not None
        assert isinstance(router._agent, ClaudeAgentSDKWrapper)

    @pytest.mark.asyncio
    async def test_router_has_run_method(self):
        """Router should have async run method."""
        from pocketclaw.agents.router import AgentRouter

        settings = Settings()
        router = AgentRouter(settings)

        assert hasattr(router, "run")

    @pytest.mark.asyncio
    async def test_router_has_stop_method(self):
        """Router should have async stop method."""
        from pocketclaw.agents.router import AgentRouter

        settings = Settings()
        router = AgentRouter(settings)

        assert hasattr(router, "stop")

        # Should not raise
        await router.stop()
