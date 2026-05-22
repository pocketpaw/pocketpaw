# Unit tests for the tool-output budget (#1160).
# Created: 2026-05-21
#
# Covers pocketpaw.tools.output_budget.cap_tool_output and its wiring into
# BaseTool._success / _error and ToolRegistry.execute:
#   - small input passes through unchanged (no regression)
#   - oversized free-form input gets a head+tail slice with an elision marker
#   - structured test/lint output gets a salient-lines extract
#   - the transform is idempotent and always respects the cap
#   - the cap reaches tools through both wired boundaries

from __future__ import annotations

import pytest

from pocketpaw.tools.output_budget import (
    TOOL_OUTPUT_CHAR_CAP,
    cap_tool_output,
)
from pocketpaw.tools.protocol import BaseTool
from pocketpaw.tools.registry import ToolRegistry

# --------------------------------------------------------------------------
# Sample blobs
# --------------------------------------------------------------------------


def _pytest_blob(passing: int = 1500) -> str:
    """A realistic pytest run: a banner, many PASSED lines, two failures,
    a short-summary section, and a final tally line."""
    lines = ["=" * 26 + " test session starts " + "=" * 26]
    lines += [f"tests/test_mod.py::test_case_{i} PASSED" for i in range(passing)]
    lines += [
        "_" * 70,
        "________________________ test_broken_thing ________________________",
        "E   AssertionError: expected 3, got 4",
        "_" * 70,
        "________________________ test_other_break _________________________",
        "E   ValueError: bad input",
        "=" * 24 + " short test summary info " + "=" * 24,
        "FAILED tests/test_mod.py::test_broken_thing - AssertionError: expected 3, got 4",
        "FAILED tests/test_mod.py::test_other_break - ValueError: bad input",
        f"2 failed, {passing} passed, 3 skipped in 5.12s",
    ]
    return "\n".join(lines)


def _ruff_blob(violations: int = 1500) -> str:
    """A realistic ruff lint run: many file:line:col diagnostics + a tally."""
    lines = [
        f"src/pkg/module_{i}.py:{i + 1}:80: E501 Line too long (105 > 100)"
        for i in range(violations)
    ]
    lines.append(f"Found {violations} errors.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# cap_tool_output — core behaviour
# --------------------------------------------------------------------------


class TestCapPassThrough:
    """Small / normal-sized output must never be touched."""

    def test_small_string_unchanged(self):
        text = "Successfully wrote 3 files to /tmp/out."
        assert cap_tool_output(text) == text

    def test_empty_string_unchanged(self):
        assert cap_tool_output("") == ""

    def test_string_exactly_at_cap_unchanged(self):
        text = "a" * TOOL_OUTPUT_CHAR_CAP
        assert cap_tool_output(text) == text

    def test_string_one_under_cap_unchanged(self):
        text = "a" * (TOOL_OUTPUT_CHAR_CAP - 1)
        assert cap_tool_output(text) == text

    def test_non_positive_cap_disables_capping(self):
        big = "x" * (TOOL_OUTPUT_CHAR_CAP * 3)
        assert cap_tool_output(big, cap=0) == big
        assert cap_tool_output(big, cap=-10) == big


class TestCapHeadTail:
    """Oversized free-form output gets a deterministic head+tail slice."""

    def test_large_input_is_capped(self):
        big = "x" * (TOOL_OUTPUT_CHAR_CAP * 4)
        out = cap_tool_output(big)
        assert len(out) <= TOOL_OUTPUT_CHAR_CAP
        assert len(out) < len(big)

    def test_large_input_has_elision_marker(self):
        big = "lorem ipsum " * 5000
        out = cap_tool_output(big)
        assert "tool output truncated" in out
        # The marker reports a positive number of elided chars.
        assert "chars elided" in out

    def test_head_and_tail_are_preserved(self):
        # Distinct head and tail markers so we can prove both survive.
        head = "HEAD_MARKER_START "
        tail = " TAIL_MARKER_END"
        big = head + ("middle filler " * 5000) + tail
        out = cap_tool_output(big)
        assert out.startswith("HEAD_MARKER_START")
        assert out.endswith("TAIL_MARKER_END")

    def test_deterministic(self):
        big = "repeatable content " * 4000
        assert cap_tool_output(big) == cap_tool_output(big)

    def test_idempotent(self):
        big = "some noisy output " * 5000
        once = cap_tool_output(big)
        twice = cap_tool_output(once)
        assert once == twice

    def test_custom_cap_is_respected(self):
        big = "y" * 40000
        out = cap_tool_output(big, cap=2000)
        assert len(out) <= 2000

    def test_tiny_cap_still_bounded(self):
        # Cap smaller than the marker — must still not exceed the cap.
        big = "z" * 5000
        out = cap_tool_output(big, cap=20)
        assert len(out) <= 20


class TestCapStructuredExtract:
    """Recognised test / lint output gets a salient-lines extract."""

    def test_pytest_output_is_capped(self):
        blob = _pytest_blob(passing=2000)
        out = cap_tool_output(blob)
        assert len(out) <= TOOL_OUTPUT_CHAR_CAP
        assert len(out) < len(blob)

    def test_pytest_extract_keeps_failures(self):
        blob = _pytest_blob(passing=2000)
        out = cap_tool_output(blob)
        # Both failing test names survive the extract.
        assert "test_broken_thing" in out
        assert "test_other_break" in out

    def test_pytest_extract_keeps_summary_line(self):
        blob = _pytest_blob(passing=2000)
        out = cap_tool_output(blob)
        assert "2 failed" in out

    def test_pytest_extract_keeps_assertion_detail(self):
        blob = _pytest_blob(passing=2000)
        out = cap_tool_output(blob)
        assert "AssertionError: expected 3, got 4" in out

    def test_pytest_extract_drops_passing_noise(self):
        blob = _pytest_blob(passing=2000)
        out = cap_tool_output(blob)
        # The thousands of individual PASSED lines are noise — dropped.
        assert "test_case_1000 PASSED" not in out

    def test_pytest_extract_reports_dropped_lines(self):
        blob = _pytest_blob(passing=2000)
        out = cap_tool_output(blob)
        assert "lines elided" in out

    def test_ruff_output_is_capped(self):
        blob = _ruff_blob(violations=2000)
        out = cap_tool_output(blob)
        assert len(out) <= TOOL_OUTPUT_CHAR_CAP
        assert len(out) < len(blob)

    def test_ruff_extract_keeps_diagnostics_and_tally(self):
        blob = _ruff_blob(violations=2000)
        out = cap_tool_output(blob)
        assert "E501 Line too long" in out
        assert "Found 2000 errors." in out

    def test_structured_extract_is_idempotent(self):
        blob = _pytest_blob(passing=2000)
        once = cap_tool_output(blob)
        twice = cap_tool_output(once)
        assert once == twice

    def test_pathological_failure_count_still_bounded(self):
        # A run where almost every line is a FAILED line — the salient
        # extract itself could blow the budget, so it must be re-capped.
        lines = [f"FAILED tests/test_mod.py::test_{i} - boom" for i in range(50000)]
        blob = "\n".join(lines)
        out = cap_tool_output(blob)
        assert len(out) <= TOOL_OUTPUT_CHAR_CAP


# --------------------------------------------------------------------------
# Wiring — the cap reaches tools through both boundaries
# --------------------------------------------------------------------------


class _SuccessTool(BaseTool):
    """Tool that returns a big payload through BaseTool._success."""

    @property
    def name(self) -> str:
        return "big_success_tool"

    @property
    def description(self) -> str:
        return "Returns a large payload via _success."

    async def execute(self, **params: object) -> str:
        return self._success("payload " * 6000)


class _ErrorTool(BaseTool):
    """Tool that returns a big payload through BaseTool._error."""

    @property
    def name(self) -> str:
        return "big_error_tool"

    @property
    def description(self) -> str:
        return "Returns a large error via _error."

    async def execute(self, **params: object) -> str:
        return self._error("stacktrace line " * 6000)


class _DirectReturnTool(BaseTool):
    """Tool that returns a big string directly — bypasses _success,
    the way ShellTool and RunPythonTool do."""

    @property
    def name(self) -> str:
        return "big_direct_tool"

    @property
    def description(self) -> str:
        return "Returns a large string without _success."

    async def execute(self, **params: object) -> str:
        return "raw command stdout " * 6000


class _SmallTool(BaseTool):
    """Tool with normal-sized output — must pass through unchanged."""

    @property
    def name(self) -> str:
        return "small_tool"

    @property
    def description(self) -> str:
        return "Returns a small string."

    async def execute(self, **params: object) -> str:
        return "ok: done"


class TestBaseToolWiring:
    """BaseTool._success / _error apply the cap at the return boundary."""

    async def test_success_payload_is_capped(self):
        out = await _SuccessTool().execute()
        assert len(out) <= TOOL_OUTPUT_CHAR_CAP

    async def test_error_payload_is_capped(self):
        out = await _ErrorTool().execute()
        assert len(out) <= TOOL_OUTPUT_CHAR_CAP
        # Still recognisable as an error.
        assert out.startswith("Error:")

    async def test_small_success_unchanged(self):
        tool = _SmallTool()
        assert await tool.execute() == "ok: done"


class TestRegistryWiring:
    """ToolRegistry.execute caps every tool, including ones that bypass
    _success by returning a string directly."""

    async def test_direct_return_tool_is_capped(self):
        registry = ToolRegistry()
        registry.register(_DirectReturnTool())
        out = await registry.execute("big_direct_tool")
        assert len(out) <= TOOL_OUTPUT_CHAR_CAP

    async def test_success_tool_through_registry_is_capped(self):
        registry = ToolRegistry()
        registry.register(_SuccessTool())
        out = await registry.execute("big_success_tool")
        assert len(out) <= TOOL_OUTPUT_CHAR_CAP

    async def test_small_tool_through_registry_unchanged(self):
        registry = ToolRegistry()
        registry.register(_SmallTool())
        out = await registry.execute("small_tool")
        assert out == "ok: done"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
