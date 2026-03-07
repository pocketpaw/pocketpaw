"""Tests for ToolRegistry — execute, timeout, policy, and audit logging.

Covers the per-tool timeout behaviour introduced to fix the issue where
``ToolRegistry.execute()`` awaited ``tool.execute(**params)`` without any
timeout, causing a hung tool to block the agent session indefinitely.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.registry import ToolRegistry

# ─── Test helpers ───────────────────────────────────────────────────────


class EchoTool(BaseTool):
    """A trivial tool that returns whatever it receives."""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echoes input."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }

    async def execute(self, message: str = "") -> str:
        return f"echo: {message}"


class SlowTool(BaseTool):
    """Tool that sleeps for a specified duration — used to test timeout.

    Uses small values (< 1 s) by default to avoid CI flakiness.
    """

    def __init__(self, delay: float = 0.5):
        self._delay = delay

    @property
    def name(self) -> str:
        return "slow"

    @property
    def description(self) -> str:
        return "Sleeps for a configured duration."

    async def execute(self, **params: Any) -> str:
        await asyncio.sleep(self._delay)
        return "done"


class CancellationTrappingTool(BaseTool):
    """Tool that traps CancelledError and tries lengthy cleanup.

    Used to verify that wait_for timeout still unblocks the caller
    even when the underlying coroutine does not exit immediately.
    """

    def __init__(self) -> None:
        self.cleanup_started = False
        self.cleanup_finished = False

    @property
    def name(self) -> str:
        return "trapper"

    @property
    def description(self) -> str:
        return "Traps cancellation to simulate slow cleanup."

    async def execute(self, **params: Any) -> str:
        try:
            await asyncio.sleep(10)  # Long sleep, will be cancelled
        except asyncio.CancelledError:
            self.cleanup_started = True
            # Simulate slow cleanup — but re-raise properly
            await asyncio.sleep(0)
            self.cleanup_finished = True
            raise
        return "should not reach"


class FailTool(BaseTool):
    """Tool that always raises an exception."""

    @property
    def name(self) -> str:
        return "fail"

    @property
    def description(self) -> str:
        return "Always fails."

    async def execute(self, **params: Any) -> str:
        raise RuntimeError("intentional failure")


class CriticalTool(BaseTool):
    """Tool with critical trust level."""

    @property
    def name(self) -> str:
        return "critical_tool"

    @property
    def description(self) -> str:
        return "A critical-trust tool."

    @property
    def trust_level(self) -> str:
        return "critical"

    async def execute(self, **params: Any) -> str:
        return "critical result"


class HighTrustTool(BaseTool):
    """Tool with high trust level."""

    @property
    def name(self) -> str:
        return "high_tool"

    @property
    def description(self) -> str:
        return "A high-trust tool."

    @property
    def trust_level(self) -> str:
        return "high"

    async def execute(self, **params: Any) -> str:
        return "high result"


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def registry() -> ToolRegistry:
    """Empty registry with no policy."""
    return ToolRegistry()


@pytest.fixture()
def _mock_audit():
    """Patch the audit logger so tests don't write to disk."""
    mock_logger = MagicMock()
    mock_logger.log_tool_use = MagicMock(return_value="mock-event-id")
    mock_logger.log = MagicMock()
    with patch("pocketpaw.tools.registry.get_audit_logger", return_value=mock_logger):
        yield mock_logger


# ─── Registration ───────────────────────────────────────────────────────


class TestRegistration:
    def test_register_and_has(self, registry: ToolRegistry):
        tool = EchoTool()
        registry.register(tool)
        assert registry.has("echo")
        assert not registry.has("nonexistent")

    def test_unregister(self, registry: ToolRegistry):
        tool = EchoTool()
        registry.register(tool)
        registry.unregister("echo")
        assert not registry.has("echo")

    def test_unregister_missing_is_noop(self, registry: ToolRegistry):
        registry.unregister("nonexistent")  # should not raise

    def test_get(self, registry: ToolRegistry):
        tool = EchoTool()
        registry.register(tool)
        assert registry.get("echo") is tool
        assert registry.get("nonexistent") is None

    def test_len(self, registry: ToolRegistry):
        assert len(registry) == 0
        registry.register(EchoTool())
        assert len(registry) == 1

    def test_tool_names(self, registry: ToolRegistry):
        registry.register(EchoTool())
        registry.register(SlowTool())
        assert set(registry.tool_names) == {"echo", "slow"}

    def test_register_overwrites(self, registry: ToolRegistry):
        tool_a = EchoTool()
        tool_b = EchoTool()
        registry.register(tool_a)
        registry.register(tool_b)
        assert registry.get("echo") is tool_b
        assert len(registry) == 1


# ─── Execution basics ──────────────────────────────────────────────────


class TestExecuteBasics:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_execute_success(self, registry: ToolRegistry):
        registry.register(EchoTool())
        with patch.object(registry, "_get_tool_timeout", return_value=0):
            result = await registry.execute("echo", message="hello")
        assert result == "echo: hello"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_execute_unknown_tool(self, registry: ToolRegistry):
        result = await registry.execute("nonexistent")
        assert "not found" in result

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_execute_error(self, registry: ToolRegistry):
        registry.register(FailTool())
        with patch.object(registry, "_get_tool_timeout", return_value=0):
            result = await registry.execute("fail")
        assert "Error executing fail" in result
        assert "intentional failure" in result

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_execute_missing_required_param(self, registry: ToolRegistry):
        registry.register(EchoTool())
        with patch.object(registry, "_get_tool_timeout", return_value=0):
            result = await registry.execute("echo")  # missing 'message'
        assert "Missing required parameter" in result


# ─── Timeout behaviour (core fix) ──────────────────────────────────────


class TestTimeout:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_tool_times_out(self, registry: ToolRegistry):
        """A tool that sleeps longer than the timeout should be cancelled."""
        registry.register(SlowTool(delay=2.0))
        with patch.object(registry, "_get_tool_timeout", return_value=0.1):
            result = await registry.execute("slow")
        assert "timed out" in result

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_timeout_returns_actionable_message(self, registry: ToolRegistry):
        """The error message should guide the agent to retry."""
        registry.register(SlowTool(delay=2.0))
        with patch.object(registry, "_get_tool_timeout", return_value=0.1):
            result = await registry.execute("slow")
        assert "retry" in result.lower() or "alternative" in result.lower()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_timeout_audit_logged(
        self, registry: ToolRegistry, _mock_audit: MagicMock
    ):
        """Timeout events should be audit-logged with status='timeout'."""
        registry.register(SlowTool(delay=2.0))
        with patch.object(registry, "_get_tool_timeout", return_value=0.1):
            await registry.execute("slow")

        # The audit.log() should have been called with a tool_timeout action.
        _mock_audit.log.assert_called()
        event = _mock_audit.log.call_args[0][0]
        assert event.action == "tool_timeout"
        assert event.status == "timeout"
        assert event.target == "slow"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_zero_timeout_disables_guard(self, registry: ToolRegistry):
        """When tool_timeout is 0, no timeout is enforced."""
        registry.register(EchoTool())
        with patch.object(registry, "_get_tool_timeout", return_value=0):
            result = await registry.execute("echo", message="no timeout")
        assert result == "echo: no timeout"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_fast_tool_completes_within_timeout(self, registry: ToolRegistry):
        """Tools that complete before the timeout should succeed normally."""
        registry.register(EchoTool())
        with patch.object(registry, "_get_tool_timeout", return_value=30):
            result = await registry.execute("echo", message="fast")
        assert result == "echo: fast"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_agent_loop_continues_after_timeout(self, registry: ToolRegistry):
        """After a timeout the registry should still be usable."""
        registry.register(SlowTool(delay=2.0))
        registry.register(EchoTool())

        with patch.object(registry, "_get_tool_timeout", return_value=0.1):
            timeout_result = await registry.execute("slow")
            ok_result = await registry.execute("echo", message="still works")

        assert "timed out" in timeout_result
        assert ok_result == "echo: still works"


# ─── Cancellation semantics ────────────────────────────────────────────


class TestCancellation:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_cancel_propagates_to_tool(self, registry: ToolRegistry):
        """When wait_for times out the underlying coroutine must be cancelled.

        Verifies the tool's CancelledError handler ran (cleanup_started)
        and that the registry returned a timeout error, not the tool result.
        """
        tool = CancellationTrappingTool()
        registry.register(tool)

        with patch.object(registry, "_get_tool_timeout", return_value=0.1):
            result = await registry.execute("trapper")

        assert "timed out" in result
        # The tool's cancellation handler should have been triggered
        assert tool.cleanup_started is True

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_cancelled_error_is_reraised(self, registry: ToolRegistry):
        """asyncio.CancelledError during execution must be re-raised.

        This ensures upstream shutdown/cancellation is not swallowed.
        """
        registry.register(EchoTool())

        async def mock_execute(**params: Any) -> str:
            raise asyncio.CancelledError()

        registry.get("echo").execute = mock_execute  # type: ignore[union-attr]

        with (
            patch.object(registry, "_get_tool_timeout", return_value=0),
            pytest.raises(asyncio.CancelledError),
        ):
            await registry.execute("echo", message="cancel me")


# ─── Concurrent timeouts ───────────────────────────────────────────────


class TestConcurrentTimeouts:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_concurrent_tool_timeouts_are_independent(
        self, registry: ToolRegistry
    ):
        """Two slow tools timing out concurrently should not interfere."""
        registry.register(SlowTool(delay=2.0))
        registry.register(EchoTool())

        with patch.object(registry, "_get_tool_timeout", return_value=0.1):
            results = await asyncio.gather(
                registry.execute("slow"),
                registry.execute("echo", message="fast"),
            )

        assert "timed out" in results[0]
        assert results[1] == "echo: fast"


# ─── Audit logging ─────────────────────────────────────────────────────


class TestAuditLogging:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_success_logs_attempt_and_success(
        self, registry: ToolRegistry, _mock_audit: MagicMock
    ):
        registry.register(EchoTool())
        with patch.object(registry, "_get_tool_timeout", return_value=0):
            await registry.execute("echo", message="hi")

        calls = _mock_audit.log_tool_use.call_args_list
        statuses = [
            c.kwargs.get("status")
            or c[1].get("status", c[0][2] if len(c[0]) > 2 else None)
            for c in calls
        ]
        # At minimum we should see attempt + success
        assert any("attempt" in str(s) for s in statuses)
        assert any("success" in str(s) for s in statuses)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_error_logs_audit_event(
        self, registry: ToolRegistry, _mock_audit: MagicMock
    ):
        registry.register(FailTool())
        with patch.object(registry, "_get_tool_timeout", return_value=0):
            await registry.execute("fail")

        _mock_audit.log.assert_called()
        event = _mock_audit.log.call_args[0][0]
        assert event.action == "tool_error"
        assert event.status == "error"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_critical_tool_severity(
        self, registry: ToolRegistry, _mock_audit: MagicMock
    ):
        from pocketpaw.security import AuditSeverity

        registry.register(CriticalTool())
        with patch.object(registry, "_get_tool_timeout", return_value=0):
            await registry.execute("critical_tool")

        calls = _mock_audit.log_tool_use.call_args_list
        assert any(
            c.kwargs.get("severity") == AuditSeverity.CRITICAL
            or (len(c[0]) > 2 and c[0][2] == AuditSeverity.CRITICAL)
            for c in calls
        )

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_high_trust_severity(
        self, registry: ToolRegistry, _mock_audit: MagicMock
    ):
        from pocketpaw.security import AuditSeverity

        registry.register(HighTrustTool())
        with patch.object(registry, "_get_tool_timeout", return_value=0):
            await registry.execute("high_tool")

        calls = _mock_audit.log_tool_use.call_args_list
        assert any(
            c.kwargs.get("severity") == AuditSeverity.WARNING
            or (len(c[0]) > 2 and c[0][2] == AuditSeverity.WARNING)
            for c in calls
        )


# ─── Policy integration ────────────────────────────────────────────────


class TestPolicyIntegration:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_audit")
    async def test_blocked_tool_returns_error(self, registry: ToolRegistry):
        from pocketpaw.tools.policy import ToolPolicy

        policy = ToolPolicy(deny=["echo"])
        registry.set_policy(policy)
        registry.register(EchoTool())

        result = await registry.execute("echo", message="blocked")
        assert "not allowed" in result

    def test_get_definitions_respects_policy(self, registry: ToolRegistry):
        from pocketpaw.tools.policy import ToolPolicy

        policy = ToolPolicy(deny=["echo"])
        registry.set_policy(policy)
        registry.register(EchoTool())

        definitions = registry.get_definitions()
        assert len(definitions) == 0

    def test_allowed_tool_names_filters(self, registry: ToolRegistry):
        from pocketpaw.tools.policy import ToolPolicy

        policy = ToolPolicy(deny=["echo"])
        registry.set_policy(policy)
        registry.register(EchoTool())
        registry.register(SlowTool())

        allowed = registry.allowed_tool_names
        assert "echo" not in allowed
        assert "slow" in allowed


# ─── _get_tool_timeout ─────────────────────────────────────────────────


class TestGetToolTimeout:
    def test_reads_from_settings(self, registry: ToolRegistry):
        mock_settings = MagicMock()
        mock_settings.tool_timeout = 42
        with patch("pocketpaw.config.get_settings", return_value=mock_settings):
            assert registry._get_tool_timeout() == 42

    def test_fallback_on_import_error(self, registry: ToolRegistry):
        with patch(
            "pocketpaw.config.get_settings",
            side_effect=Exception("settings unavailable"),
        ):
            assert registry._get_tool_timeout() == 60  # _DEFAULT_TOOL_TIMEOUT

    def test_negative_timeout_treated_as_zero(self, registry: ToolRegistry):
        """Negative timeout should be clamped to 0 (disabled)."""
        mock_settings = MagicMock()
        mock_settings.tool_timeout = -10
        with patch("pocketpaw.config.get_settings", return_value=mock_settings):
            assert registry._get_tool_timeout() == 0

    def test_none_timeout_treated_as_zero(self, registry: ToolRegistry):
        """None timeout should be treated as 0 (disabled)."""
        mock_settings = MagicMock()
        mock_settings.tool_timeout = None
        with patch("pocketpaw.config.get_settings", return_value=mock_settings):
            assert registry._get_tool_timeout() == 0


# ─── Config integration ────────────────────────────────────────────────


class TestConfigIntegration:
    def test_tool_timeout_default(self):
        """Settings.tool_timeout should default to 60."""
        from pocketpaw.config import Settings

        settings = Settings()
        assert settings.tool_timeout == 60

    def test_tool_timeout_custom(self):
        """Settings.tool_timeout should accept custom values."""
        from pocketpaw.config import Settings

        settings = Settings(tool_timeout=120)
        assert settings.tool_timeout == 120

    def test_tool_timeout_disable(self):
        """Settings.tool_timeout=0 should be valid (disables timeout)."""
        from pocketpaw.config import Settings

        settings = Settings(tool_timeout=0)
        assert settings.tool_timeout == 0

    def test_tool_timeout_rejects_negative(self):
        """Settings should reject negative tool_timeout at validation time."""
        from pydantic import ValidationError

        from pocketpaw.config import Settings

        with pytest.raises(ValidationError):
            Settings(tool_timeout=-1)
