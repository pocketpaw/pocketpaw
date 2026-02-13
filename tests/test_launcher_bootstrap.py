# Tests for installer/launcher/bootstrap.py
# Covers: Python detection, venv creation, pocketpaw install, version checks.
# Created: 2026-02-10

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from installer.launcher.bootstrap import (
    PACKAGE_NAME,
    Bootstrap,
)

# ── check_status ──────────────────────────────────────────────────────


class TestCheckStatus:
    """Tests for Bootstrap.check_status()."""

    def test_no_python_no_venv(self, tmp_path: Path):
        """When no Python found and no venv exists, needs_install is True."""
        with (
            patch.object(Bootstrap, "_find_python", return_value=None),
            patch("installer.launcher.bootstrap.VENV_DIR", tmp_path / "venv"),
        ):
            b = Bootstrap()
            status = b.check_status()
            assert status.needs_install is True
            assert status.pocketpaw_installed is False

    def test_venv_exists_with_pocketpaw(self, tmp_path: Path):
        """When venv exists and pocketpaw is installed, needs_install is False."""
        venv_dir = tmp_path / "venv"
        venv_bin = venv_dir / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.touch()

        with (
            patch.object(Bootstrap, "_find_python", return_value=str(python)),
            patch.object(Bootstrap, "_get_python_version", return_value="3.12.8"),
            patch("installer.launcher.bootstrap.VENV_DIR", venv_dir),
            patch.object(Bootstrap, "_get_installed_version", return_value="0.2.5"),
        ):
            b = Bootstrap()
            status = b.check_status()
            assert status.needs_install is False
            assert status.pocketpaw_installed is True
            assert status.pocketpaw_version == "0.2.5"

    def test_venv_exists_no_pocketpaw(self, tmp_path: Path):
        """When venv exists but pocketpaw is not installed."""
        venv_dir = tmp_path / "venv"
        venv_bin = venv_dir / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.touch()

        with (
            patch.object(Bootstrap, "_find_python", return_value=str(python)),
            patch.object(Bootstrap, "_get_python_version", return_value="3.12.8"),
            patch("installer.launcher.bootstrap.VENV_DIR", venv_dir),
            patch.object(Bootstrap, "_get_installed_version", return_value=None),
        ):
            b = Bootstrap()
            status = b.check_status()
            assert status.needs_install is True
            assert status.pocketpaw_installed is False


# ── _check_python_version ─────────────────────────────────────────────


class TestCheckPythonVersion:
    """Tests for Bootstrap._check_python_version()."""

    def test_valid_python_312(self):
        """Python 3.12 should pass."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "3 12\n"

        with patch("subprocess.run", return_value=mock_result):
            b = Bootstrap()
            assert b._check_python_version("/usr/bin/python3") is True

    def test_valid_python_311(self):
        """Python 3.11 should pass (minimum)."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "3 11\n"

        with patch("subprocess.run", return_value=mock_result):
            b = Bootstrap()
            assert b._check_python_version("/usr/bin/python3") is True

    def test_old_python_310(self):
        """Python 3.10 should fail."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "3 10\n"

        with patch("subprocess.run", return_value=mock_result):
            b = Bootstrap()
            assert b._check_python_version("/usr/bin/python3") is False

    def test_python_not_found(self):
        """Missing Python binary should return False."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            b = Bootstrap()
            assert b._check_python_version("/usr/bin/nope") is False

    def test_python_timeout(self):
        """Hung Python should return False."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 10)):
            b = Bootstrap()
            assert b._check_python_version("/usr/bin/python3") is False


# ── _get_installed_version ────────────────────────────────────────────


class TestGetInstalledVersion:
    """Tests for Bootstrap._get_installed_version()."""

    def test_package_installed(self):
        """Should parse version from pip show output."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Name: pocketpaw\nVersion: 0.2.5\nSummary: A self-hosted AI agent\n"

        with patch("subprocess.run", return_value=mock_result):
            b = Bootstrap()
            assert b._get_installed_version("/path/to/python") == "0.2.5"

    def test_package_not_installed(self):
        """Should return None when package isn't installed."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            b = Bootstrap()
            assert b._get_installed_version("/path/to/python") is None

    def test_pip_timeout(self):
        """Should return None on timeout."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            b = Bootstrap()
            assert b._get_installed_version("/path/to/python") is None


# ── run (full bootstrap) ──────────────────────────────────────────────


class TestBootstrapRun:
    """Tests for Bootstrap.run() — the full bootstrap flow."""

    def test_successful_install(self, tmp_path: Path):
        """Full bootstrap with all steps succeeding."""
        venv_dir = tmp_path / "venv"
        venv_bin = venv_dir / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.touch()

        progress_calls = []

        def track_progress(msg: str, pct: int) -> None:
            progress_calls.append((msg, pct))

        with (
            patch.object(Bootstrap, "_find_python", return_value="/usr/bin/python3"),
            patch.object(Bootstrap, "_get_python_version", return_value="3.12.8"),
            patch("installer.launcher.bootstrap.VENV_DIR", venv_dir),
            patch.object(Bootstrap, "_create_venv"),
            patch.object(Bootstrap, "_upgrade_pip"),
            patch.object(Bootstrap, "_install_pocketpaw", return_value=True),
            patch.object(Bootstrap, "_get_installed_version", return_value="0.2.5"),
        ):
            b = Bootstrap(progress=track_progress)
            status = b.run(extras=["recommended"])

            assert status.error is None
            assert status.pocketpaw_installed is True
            assert status.pocketpaw_version == "0.2.5"
            assert status.needs_install is False
            # Progress was called
            assert len(progress_calls) > 0
            assert progress_calls[-1][1] == 100

    def test_no_python_found(self):
        """Should return error when no Python found (non-Windows)."""
        with (
            patch.object(Bootstrap, "_find_python", return_value=None),
            patch("platform.system", return_value="Linux"),
        ):
            b = Bootstrap()
            status = b.run()
            assert status.error is not None
            assert "Python 3.11+" in status.error

    def test_install_failure(self, tmp_path: Path):
        """Should return error when pip install fails."""
        venv_dir = tmp_path / "venv"
        venv_bin = venv_dir / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.touch()

        with (
            patch.object(Bootstrap, "_find_python", return_value="/usr/bin/python3"),
            patch.object(Bootstrap, "_get_python_version", return_value="3.12.8"),
            patch("installer.launcher.bootstrap.VENV_DIR", venv_dir),
            patch.object(Bootstrap, "_create_venv"),
            patch.object(Bootstrap, "_upgrade_pip"),
            patch.object(Bootstrap, "_install_pocketpaw", return_value=False),
        ):
            b = Bootstrap()
            status = b.run()
            assert status.error is not None
            assert "Failed to install" in status.error


# ── _install_pocketpaw ────────────────────────────────────────────────


class TestInstallPocketpaw:
    """Tests for Bootstrap._install_pocketpaw()."""

    def test_install_with_extras(self):
        """Should build correct pip command with extras."""
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            b = Bootstrap()
            result = b._install_pocketpaw("/path/to/python", ["telegram", "discord"])

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert f"{PACKAGE_NAME}[telegram,discord]" in call_args

    def test_install_no_extras(self):
        """Should install bare package when no extras."""
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            b = Bootstrap()
            result = b._install_pocketpaw("/path/to/python", [])

            assert result is True
            call_args = mock_run.call_args[0][0]
            assert PACKAGE_NAME in call_args

    def test_install_pip_failure(self):
        """Should return False on pip failure."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "ERROR: Could not find a version"

        with patch("subprocess.run", return_value=mock_result):
            b = Bootstrap()
            result = b._install_pocketpaw("/path/to/python", [])
            assert result is False
