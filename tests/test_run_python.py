# Tests for RunPythonTool - sandboxed Python execution.
# Created: 2026-03-12

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_guardian_safe():
    """Return a guardian mock that approves all code."""
    guardian = MagicMock()
    guardian.check_command = AsyncMock(return_value=(True, ""))
    return guardian


@pytest.fixture
def jail(tmp_path):
    """Return a real Path used as the file jail, pointing to tmp_path."""
    return tmp_path


@pytest.fixture
def mock_settings(jail):
    """Return a settings mock with file_jail_path pointing to tmp_path."""
    settings = MagicMock()
    settings.file_jail_path = jail
    return settings


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_python_basic(mock_guardian_safe, mock_settings):
    """print('hello') should produce 'hello' in the output."""
    from pocketpaw.tools.builtin.python_exec import RunPythonTool

    with (
        patch("pocketpaw.tools.builtin.python_exec.get_guardian", return_value=mock_guardian_safe),
        patch("pocketpaw.tools.builtin.python_exec.get_settings", return_value=mock_settings),
    ):
        tool = RunPythonTool()
        result = await tool.execute(code='print("hello")')

    assert "hello" in result


@pytest.mark.asyncio
async def test_run_python_multiline(mock_guardian_safe, mock_settings):
    """Multi-line script with stdlib import should run correctly."""
    from pocketpaw.tools.builtin.python_exec import RunPythonTool

    code = "import math\nresult = math.sqrt(9)\nprint(f'sqrt={result}')"

    with (
        patch("pocketpaw.tools.builtin.python_exec.get_guardian", return_value=mock_guardian_safe),
        patch("pocketpaw.tools.builtin.python_exec.get_settings", return_value=mock_settings),
    ):
        tool = RunPythonTool()
        result = await tool.execute(code=code)

    assert "sqrt=3.0" in result


@pytest.mark.asyncio
async def test_run_python_stderr(mock_guardian_safe, mock_settings):
    """Code that writes to stderr should have STDERR section in output."""
    from pocketpaw.tools.builtin.python_exec import RunPythonTool

    code = "import sys\nsys.stderr.write('boom\\n')"

    with (
        patch("pocketpaw.tools.builtin.python_exec.get_guardian", return_value=mock_guardian_safe),
        patch("pocketpaw.tools.builtin.python_exec.get_settings", return_value=mock_settings),
    ):
        tool = RunPythonTool()
        result = await tool.execute(code=code)

    assert "STDERR" in result
    assert "boom" in result


@pytest.mark.asyncio
async def test_run_python_exit_code(mock_guardian_safe, mock_settings):
    """sys.exit(1) should surface 'Exit code: 1' in the output."""
    from pocketpaw.tools.builtin.python_exec import RunPythonTool

    code = "import sys\nsys.exit(1)"

    with (
        patch("pocketpaw.tools.builtin.python_exec.get_guardian", return_value=mock_guardian_safe),
        patch("pocketpaw.tools.builtin.python_exec.get_settings", return_value=mock_settings),
    ):
        tool = RunPythonTool()
        result = await tool.execute(code=code)

    assert "Exit code: 1" in result


@pytest.mark.asyncio
async def test_run_python_timeout(mock_guardian_safe, mock_settings):
    """Infinite loop with timeout=1 should return a timed-out error."""
    from pocketpaw.tools.builtin.python_exec import RunPythonTool

    code = "while True: pass"

    with (
        patch("pocketpaw.tools.builtin.python_exec.get_guardian", return_value=mock_guardian_safe),
        patch("pocketpaw.tools.builtin.python_exec.get_settings", return_value=mock_settings),
    ):
        tool = RunPythonTool()
        result = await tool.execute(code=code, timeout=1)

    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_run_python_syntax_error(mock_guardian_safe, mock_settings):
    """Invalid Python should produce a SyntaxError in stderr."""
    from pocketpaw.tools.builtin.python_exec import RunPythonTool

    code = "def broken(:"  # deliberate syntax error

    with (
        patch("pocketpaw.tools.builtin.python_exec.get_guardian", return_value=mock_guardian_safe),
        patch("pocketpaw.tools.builtin.python_exec.get_settings", return_value=mock_settings),
    ):
        tool = RunPythonTool()
        result = await tool.execute(code=code)

    # Python writes SyntaxError to stderr and exits non-zero
    assert "SyntaxError" in result or "Error" in result


# ---------------------------------------------------------------------------
# Security tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_python_guardian_block(mock_settings):
    """Guardian returning (False, 'blocked') should prevent execution."""
    from pocketpaw.tools.builtin.python_exec import RunPythonTool

    blocking_guardian = MagicMock()
    blocking_guardian.check_command = AsyncMock(return_value=(False, "blocked by policy"))

    with (
        patch("pocketpaw.tools.builtin.python_exec.get_guardian", return_value=blocking_guardian),
        patch("pocketpaw.tools.builtin.python_exec.get_settings", return_value=mock_settings),
    ):
        tool = RunPythonTool()
        result = await tool.execute(code='print("hello")')

    assert "blocked" in result.lower()


# ---------------------------------------------------------------------------
# File system / isolation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_python_file_creation(mock_guardian_safe, mock_settings, jail):
    """Script that creates a file in cwd should leave that file in the jail."""
    from pocketpaw.tools.builtin.python_exec import RunPythonTool

    code = "with open('output.txt', 'w') as f:\n    f.write('created')"

    with (
        patch("pocketpaw.tools.builtin.python_exec.get_guardian", return_value=mock_guardian_safe),
        patch("pocketpaw.tools.builtin.python_exec.get_settings", return_value=mock_settings),
    ):
        tool = RunPythonTool()
        await tool.execute(code=code)

    assert (jail / "output.txt").exists()
    assert (jail / "output.txt").read_text() == "created"


@pytest.mark.asyncio
async def test_run_python_cleanup(mock_guardian_safe, mock_settings, jail):
    """Temp script file should be removed after execution completes."""
    from pocketpaw.tools.builtin.python_exec import RunPythonTool

    with (
        patch("pocketpaw.tools.builtin.python_exec.get_guardian", return_value=mock_guardian_safe),
        patch("pocketpaw.tools.builtin.python_exec.get_settings", return_value=mock_settings),
    ):
        tool = RunPythonTool()
        await tool.execute(code='print("cleanup test")')

    # No _pocketpaw_run_*.py files should remain
    leftover = list(jail.glob("_pocketpaw_run_*.py"))
    assert leftover == [], f"Temp script files not cleaned up: {leftover}"


# ---------------------------------------------------------------------------
# Definition / metadata tests
# ---------------------------------------------------------------------------


def test_run_python_definition():
    """Tool definition should have correct name, trust level, and parameters."""
    from pocketpaw.tools.builtin.python_exec import RunPythonTool

    tool = RunPythonTool()
    defn = tool.definition

    assert defn.name == "run_python"
    assert defn.trust_level == "elevated"

    props = defn.parameters["properties"]
    assert "code" in props
    assert "timeout" in props
    assert "code" in defn.parameters["required"]
    assert "timeout" not in defn.parameters.get("required", [])
