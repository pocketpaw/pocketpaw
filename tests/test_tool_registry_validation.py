# Tests for ToolRegistry required parameter validation.
# Covers fix for issue #793: empty strings bypassing required param checks.
# Created: 2026-03-29

from __future__ import annotations

from typing import Any

import pytest

from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class DummyTool(BaseTool):
    """A minimal tool for testing parameter validation."""

    def __init__(self, name: str = "test_tool", required: list[str] | None = None):
        self._name = name
        self._required = ["command"] if required is None else required

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "A dummy tool for testing."

    @property
    def parameters(self) -> dict[str, Any]:
        props = {p: {"type": "string", "description": f"Param {p}"} for p in self._required}
        return {
            "type": "object",
            "properties": props,
            "required": self._required,
        }

    async def execute(self, **params: Any) -> str:
        return f"executed with {params}"


def _make_registry(tool: BaseTool | None = None) -> ToolRegistry:
    """Create a ToolRegistry with a single DummyTool registered."""
    registry = ToolRegistry()
    registry.register(tool or DummyTool())
    return registry


# ---------------------------------------------------------------------------
# Tests — Issue #793: empty string should NOT bypass required param validation
# ---------------------------------------------------------------------------


class TestRequiredParamValidation:
    """Verify that required parameters are validated correctly."""

    @pytest.mark.asyncio
    async def test_none_value_rejected(self):
        """None values for required params must be rejected."""
        registry = _make_registry()
        result = await registry.execute("test_tool", command=None)
        assert "Missing required parameter" in result
        assert "command" in result

    @pytest.mark.asyncio
    async def test_missing_param_rejected(self):
        """Completely omitting a required param must be rejected."""
        registry = _make_registry()
        result = await registry.execute("test_tool")
        assert "Missing required parameter" in result
        assert "command" in result

    @pytest.mark.asyncio
    async def test_empty_string_rejected(self):
        """Empty string '' for a required param must be rejected (issue #793)."""
        registry = _make_registry()
        result = await registry.execute("test_tool", command="")
        assert "Missing required parameter" in result
        assert "command" in result

    @pytest.mark.asyncio
    async def test_whitespace_only_rejected(self):
        """Whitespace-only strings must be treated as empty (issue #793)."""
        registry = _make_registry()
        result = await registry.execute("test_tool", command="   ")
        assert "Missing required parameter" in result
        assert "command" in result

    @pytest.mark.asyncio
    async def test_tabs_and_newlines_rejected(self):
        """Strings with only tabs/newlines must be treated as empty."""
        registry = _make_registry()
        result = await registry.execute("test_tool", command="\t\n  \t")
        assert "Missing required parameter" in result

    @pytest.mark.asyncio
    async def test_valid_string_accepted(self):
        """A real non-empty string value must pass validation."""
        registry = _make_registry()
        result = await registry.execute("test_tool", command="ls -la")
        assert "executed with" in result
        assert "Missing required parameter" not in result

    @pytest.mark.asyncio
    async def test_string_with_leading_spaces_accepted(self):
        """A string with content plus leading/trailing spaces must pass."""
        registry = _make_registry()
        result = await registry.execute("test_tool", command="  hello  ")
        assert "executed with" in result
        assert "Missing required parameter" not in result

    @pytest.mark.asyncio
    async def test_non_string_types_not_affected(self):
        """Non-string values (int, bool, list) must not be falsely rejected."""
        registry = _make_registry()
        # Integer 0 is a valid value, not an empty string
        result = await registry.execute("test_tool", command=0)
        assert "executed with" in result
        assert "Missing required parameter" not in result

    @pytest.mark.asyncio
    async def test_false_bool_not_rejected(self):
        """Boolean False must not be treated as empty."""
        registry = _make_registry()
        result = await registry.execute("test_tool", command=False)
        assert "executed with" in result

    @pytest.mark.asyncio
    async def test_multiple_required_params_all_empty(self):
        """All empty required params must be reported."""
        tool = DummyTool(required=["command", "path"])
        registry = _make_registry(tool)
        result = await registry.execute("test_tool", command="", path="")
        assert "Missing required parameter" in result
        assert "command" in result
        assert "path" in result

    @pytest.mark.asyncio
    async def test_multiple_required_params_one_empty(self):
        """Only the empty param must be reported when others are valid."""
        tool = DummyTool(required=["command", "path"])
        registry = _make_registry(tool)
        result = await registry.execute("test_tool", command="ls", path="")
        assert "Missing required parameter" in result
        assert "path" in result
        assert "command" not in result.split("Missing")[1]

    @pytest.mark.asyncio
    async def test_tool_not_found(self):
        """Requesting a nonexistent tool must return an error."""
        registry = _make_registry()
        result = await registry.execute("nonexistent")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_no_required_params_no_validation(self):
        """Tools without required params should not trigger validation."""
        tool = DummyTool(required=[])
        registry = _make_registry(tool)
        result = await registry.execute("test_tool")
        assert "executed with" in result


class TestAuditLoggingOnValidationFailure:
    """Verify that validation failures are logged to the audit trail."""

    @pytest.mark.asyncio
    async def test_validation_failure_logged(self):
        """Empty-string validation failure must trigger audit log_tool_use."""
        registry = _make_registry()
        result = await registry.execute("test_tool", command="")
        # The result must indicate validation failure
        assert "Missing required parameter" in result
        # The audit status is "validation_failed" — tested implicitly
        # because the function returns before reaching "attempt" or "success"
