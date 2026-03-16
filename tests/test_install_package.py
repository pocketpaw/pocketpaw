# Tests for InstallPackageTool - pip install with Guardian review.
# Created: 2026-03-12

import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_guardian():
    """Guardian that approves all commands by default."""
    guardian = MagicMock()
    guardian.check_command = AsyncMock(return_value=(True, "Looks safe"))
    return guardian


@pytest.fixture
def successful_pip_result():
    """Subprocess result simulating a successful pip install."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = "Successfully installed requests-2.31.0"
    result.stderr = ""
    return result


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_package_basic(mock_guardian, successful_pip_result):
    """A basic install should return pip's stdout on success."""
    from pocketpaw.tools.builtin.pip_install import InstallPackageTool

    with (
        patch("pocketpaw.tools.builtin.pip_install.get_guardian", return_value=mock_guardian),
        patch("subprocess.run", return_value=successful_pip_result),
    ):
        tool = InstallPackageTool()
        result = await tool.execute(package="requests")

    assert "Successfully installed" in result
    assert "Error" not in result


@pytest.mark.asyncio
async def test_install_package_with_version(mock_guardian):
    """Version specifier should be passed through to pip unchanged."""
    from pocketpaw.tools.builtin.pip_install import InstallPackageTool

    captured: list[list[str]] = []

    def capture_run(cmd, **kwargs):
        captured.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = "Successfully installed requests-2.31.0"
        result.stderr = ""
        return result

    with (
        patch("pocketpaw.tools.builtin.pip_install.get_guardian", return_value=mock_guardian),
        patch("subprocess.run", side_effect=capture_run),
    ):
        tool = InstallPackageTool()
        await tool.execute(package="requests>=2.28.0")

    assert len(captured) == 1
    cmd = captured[0]
    assert "requests>=2.28.0" in cmd


@pytest.mark.asyncio
async def test_install_package_with_extras(mock_guardian):
    """Bracket extras like pocketpaw[soul] should be allowed and forwarded to pip."""
    from pocketpaw.tools.builtin.pip_install import InstallPackageTool

    captured: list[list[str]] = []

    def capture_run(cmd, **kwargs):
        captured.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = "Successfully installed pocketpaw-0.4.4"
        result.stderr = ""
        return result

    with (
        patch("pocketpaw.tools.builtin.pip_install.get_guardian", return_value=mock_guardian),
        patch("subprocess.run", side_effect=capture_run),
    ):
        tool = InstallPackageTool()
        result = await tool.execute(package="pocketpaw[soul]")

    assert "Error" not in result
    cmd = captured[0]
    assert "pocketpaw[soul]" in cmd


@pytest.mark.asyncio
async def test_install_package_upgrade(mock_guardian):
    """upgrade=True should add --upgrade flag to the pip command."""
    from pocketpaw.tools.builtin.pip_install import InstallPackageTool

    captured: list[list[str]] = []

    def capture_run(cmd, **kwargs):
        captured.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stdout = "Successfully installed pip-24.0"
        result.stderr = ""
        return result

    with (
        patch("pocketpaw.tools.builtin.pip_install.get_guardian", return_value=mock_guardian),
        patch("subprocess.run", side_effect=capture_run),
    ):
        tool = InstallPackageTool()
        await tool.execute(package="pip", upgrade=True)

    cmd = captured[0]
    assert "--upgrade" in cmd


# ---------------------------------------------------------------------------
# Shell injection blocking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_package_shell_injection_semicolon(mock_guardian):
    """Semicolons in the package name must be rejected before Guardian or pip runs."""
    from pocketpaw.tools.builtin.pip_install import InstallPackageTool

    with patch("pocketpaw.tools.builtin.pip_install.get_guardian", return_value=mock_guardian):
        tool = InstallPackageTool()
        result = await tool.execute(package="foo; rm -rf /")

    assert result.startswith("Error:")
    # Guardian should never have been called since validation happens first
    mock_guardian.check_command.assert_not_called()


@pytest.mark.asyncio
async def test_install_package_shell_injection_pipe(mock_guardian):
    """Pipes in the package name must be blocked by input validation."""
    from pocketpaw.tools.builtin.pip_install import InstallPackageTool

    with patch("pocketpaw.tools.builtin.pip_install.get_guardian", return_value=mock_guardian):
        tool = InstallPackageTool()
        result = await tool.execute(package="foo | cat /etc/passwd")

    assert result.startswith("Error:")
    mock_guardian.check_command.assert_not_called()


@pytest.mark.asyncio
async def test_install_package_shell_injection_backtick(mock_guardian):
    """Backtick command substitution in the package name must be blocked."""
    from pocketpaw.tools.builtin.pip_install import InstallPackageTool

    with patch("pocketpaw.tools.builtin.pip_install.get_guardian", return_value=mock_guardian):
        tool = InstallPackageTool()
        result = await tool.execute(package="foo`whoami`")

    assert result.startswith("Error:")
    mock_guardian.check_command.assert_not_called()


# ---------------------------------------------------------------------------
# Guardian blocking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_package_guardian_block():
    """When Guardian flags a package, installation must be aborted."""
    from pocketpaw.tools.builtin.pip_install import InstallPackageTool

    blocking_guardian = MagicMock()
    blocking_guardian.check_command = AsyncMock(
        return_value=(False, "suspicious package, possible typosquatting")
    )

    with (
        patch("pocketpaw.tools.builtin.pip_install.get_guardian", return_value=blocking_guardian),
        patch("subprocess.run") as mock_run,
    ):
        tool = InstallPackageTool()
        result = await tool.execute(package="reqeusts")  # deliberate typo

    assert result.startswith("Error:")
    assert "Guardian" in result
    # pip should never run if Guardian blocks
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Error conditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_package_timeout(mock_guardian):
    """A subprocess timeout should be reported cleanly as an error."""
    from pocketpaw.tools.builtin.pip_install import InstallPackageTool

    def raise_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=300)

    with (
        patch("pocketpaw.tools.builtin.pip_install.get_guardian", return_value=mock_guardian),
        patch("subprocess.run", side_effect=raise_timeout),
    ):
        tool = InstallPackageTool(timeout=300)
        result = await tool.execute(package="some-large-package")

    assert result.startswith("Error:")
    assert "timed out" in result


@pytest.mark.asyncio
async def test_install_package_pip_failure(mock_guardian):
    """A non-zero pip exit code should surface as an error with stderr content."""
    from pocketpaw.tools.builtin.pip_install import InstallPackageTool

    fail_result = MagicMock()
    fail_result.returncode = 1
    fail_result.stdout = ""
    fail_result.stderr = "ERROR: Could not find a version that satisfies the requirement nosuchpkg"

    with (
        patch("pocketpaw.tools.builtin.pip_install.get_guardian", return_value=mock_guardian),
        patch("subprocess.run", return_value=fail_result),
    ):
        tool = InstallPackageTool()
        result = await tool.execute(package="nosuchpkg")

    assert result.startswith("Error:")
    assert "Could not find" in result


# ---------------------------------------------------------------------------
# Tool definition / metadata
# ---------------------------------------------------------------------------


def test_install_package_definition():
    """Tool definition should expose the correct name, trust level, and parameter schema."""
    from pocketpaw.tools.builtin.pip_install import InstallPackageTool

    tool = InstallPackageTool()
    defn = tool.definition

    assert defn.name == "install_package"
    assert defn.trust_level == "elevated"

    props = defn.parameters["properties"]
    assert "package" in props
    assert "upgrade" in props

    required = defn.parameters["required"]
    assert "package" in required
    assert "upgrade" not in required

    # upgrade should default to False
    assert props["upgrade"]["default"] is False
