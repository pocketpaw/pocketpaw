# Tests for per-tool execution timeout (Issue #494).

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.tools.protocol import BaseTool

# ---------- Helpers ----------


class FastTool(BaseTool):
    """A tool that returns immediately."""

    @property
    def name(self) -> str:
        return "fast_tool"

    @property
    def description(self) -> str:
        return "Returns instantly"

    async def execute(self, **params) -> str:
        return "done"


class SlowTool(BaseTool):
    """A tool that hangs longer than the timeout."""

    @property
    def name(self) -> str:
        return "slow_tool"

    @property
    def description(self) -> str:
        return "Hangs forever"

    async def execute(self, **params) -> str:
        await asyncio.sleep(999)
        return "never"


# ---------- Fixtures ----------


def _make_mock_settings(timeout: int = 1) -> MagicMock:
    """Create a mock settings object with the given tool_timeout."""
    mock = MagicMock()
    mock.tool_timeout = timeout
    mock.injection_scan_enabled = False
    return mock


@pytest.fixture
def mock_audit():
    """Patch the audit logger where registry.py calls it."""
    mock = MagicMock()
    # Patch at the call site in registry, not at the definition site
    with patch("pocketpaw.tools.registry.get_audit_logger", return_value=mock):
        yield mock


# ---------- Tests ----------


async def test_fast_tool_succeeds_with_timeout(mock_audit):
    """A fast tool should complete normally when timeout is set."""
    from pocketpaw.tools.registry import ToolRegistry

    with patch("pocketpaw.config.get_settings", return_value=_make_mock_settings(1)):
        registry = ToolRegistry()
        registry.register(FastTool())
        result = await registry.execute("fast_tool")

    assert result == "done"


async def test_slow_tool_times_out(mock_audit):
    """A tool that exceeds the timeout should be cancelled and return an error."""
    from pocketpaw.tools.registry import ToolRegistry

    with patch("pocketpaw.config.get_settings", return_value=_make_mock_settings(1)):
        registry = ToolRegistry()
        registry.register(SlowTool())
        result = await registry.execute("slow_tool")

    assert "timed out" in result
    assert "1s" in result

    # Verify audit log was called with timeout status
    mock_audit.log.assert_called()
    audit_event = mock_audit.log.call_args[0][0]
    assert audit_event.status == "timeout"
    assert audit_event.action == "tool_timeout"


async def test_no_timeout_when_zero(mock_audit):
    """Setting tool_timeout=0 should disable the timeout (fast tool completes)."""
    from pocketpaw.tools.registry import ToolRegistry

    with patch("pocketpaw.config.get_settings", return_value=_make_mock_settings(0)):
        registry = ToolRegistry()
        registry.register(FastTool())
        result = await registry.execute("fast_tool")

    assert result == "done"
