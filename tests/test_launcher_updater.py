# Tests for installer/launcher/updater.py
# Covers: PyPI version checking, version comparison, update flow.
# Created: 2026-02-10

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from installer.launcher.updater import Updater

# ── Version Comparison ────────────────────────────────────────────────


class TestVersionComparison:
    """Tests for Updater._version_newer()."""

    def test_newer_patch(self):
        u = Updater()
        assert u._version_newer("0.2.6", "0.2.5") is True

    def test_newer_minor(self):
        u = Updater()
        assert u._version_newer("0.3.0", "0.2.5") is True

    def test_newer_major(self):
        u = Updater()
        assert u._version_newer("1.0.0", "0.9.9") is True

    def test_same_version(self):
        u = Updater()
        assert u._version_newer("0.2.5", "0.2.5") is False

    def test_older_version(self):
        u = Updater()
        assert u._version_newer("0.2.4", "0.2.5") is False

    def test_three_vs_two_segments(self):
        u = Updater()
        assert u._version_newer("0.3.0", "0.2") is True


# ── PyPI Check ────────────────────────────────────────────────────────


class TestPyPICheck:
    """Tests for Updater._get_pypi_version()."""

    def test_successful_fetch(self):
        """Should parse version from PyPI JSON response."""
        pypi_data = json.dumps({"info": {"version": "0.3.0"}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = pypi_data

        with patch("urllib.request.urlopen", return_value=mock_resp):
            u = Updater()
            assert u._get_pypi_version() == "0.3.0"

    def test_network_error(self):
        """Should return None on network failure."""
        with patch("urllib.request.urlopen", side_effect=ConnectionError):
            u = Updater()
            assert u._get_pypi_version() is None

    def test_invalid_json(self):
        """Should return None on bad JSON."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            u = Updater()
            assert u._get_pypi_version() is None


# ── Installed Version ─────────────────────────────────────────────────


class TestInstalledVersion:
    """Tests for Updater._get_installed_version()."""

    def test_installed(self, tmp_path: Path):
        """Should return version when installed."""
        venv_dir = tmp_path / "venv"
        venv_bin = venv_dir / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.touch()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Name: pocketpaw\nVersion: 0.2.5\n"

        with (
            patch("installer.launcher.updater.VENV_DIR", venv_dir),
            patch("subprocess.run", return_value=mock_result),
        ):
            u = Updater()
            assert u._get_installed_version() == "0.2.5"

    def test_not_installed(self, tmp_path: Path):
        """Should return None when venv doesn't exist."""
        with patch("installer.launcher.updater.VENV_DIR", tmp_path / "novenv"):
            u = Updater()
            assert u._get_installed_version() is None


# ── Full Check Flow ───────────────────────────────────────────────────


class TestCheckFlow:
    """Tests for Updater.check() — the full check flow."""

    def test_update_available(self):
        """Should detect when update is available."""
        with (
            patch.object(Updater, "_get_installed_version", return_value="0.2.5"),
            patch.object(Updater, "_get_pypi_version", return_value="0.3.0"),
        ):
            u = Updater()
            info = u.check()
            assert info.update_available is True
            assert info.current_version == "0.2.5"
            assert info.latest_version == "0.3.0"

    def test_up_to_date(self):
        """Should report up to date when versions match."""
        with (
            patch.object(Updater, "_get_installed_version", return_value="0.2.5"),
            patch.object(Updater, "_get_pypi_version", return_value="0.2.5"),
        ):
            u = Updater()
            info = u.check()
            assert info.update_available is False

    def test_not_installed_error(self):
        """Should report error when pocketpaw not installed."""
        with patch.object(Updater, "_get_installed_version", return_value=None):
            u = Updater()
            info = u.check()
            assert info.error is not None
            assert "not installed" in info.error.lower()

    def test_pypi_unreachable(self):
        """Should report error when PyPI is unreachable."""
        with (
            patch.object(Updater, "_get_installed_version", return_value="0.2.5"),
            patch.object(Updater, "_get_pypi_version", return_value=None),
        ):
            u = Updater()
            info = u.check()
            assert info.error is not None
            assert "pypi" in info.error.lower()


# ── Apply Update ──────────────────────────────────────────────────────


class TestApplyUpdate:
    """Tests for Updater.apply()."""

    def test_successful_upgrade(self, tmp_path: Path):
        """Should run pip upgrade and report new version."""
        venv_dir = tmp_path / "venv"
        venv_bin = venv_dir / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.touch()

        mock_result = MagicMock()
        mock_result.returncode = 0

        status_messages = []

        with (
            patch("installer.launcher.updater.VENV_DIR", venv_dir),
            patch("subprocess.run", return_value=mock_result),
            patch.object(Updater, "_get_installed_version", return_value="0.3.0"),
        ):
            u = Updater(on_status=status_messages.append)
            assert u.apply() is True
            assert any("0.3.0" in m for m in status_messages)

    def test_upgrade_failure(self, tmp_path: Path):
        """Should return False on pip failure."""
        venv_dir = tmp_path / "venv"
        venv_bin = venv_dir / "bin"
        venv_bin.mkdir(parents=True)
        python = venv_bin / "python"
        python.touch()

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "ERROR: some pip error"

        with (
            patch("installer.launcher.updater.VENV_DIR", venv_dir),
            patch("subprocess.run", return_value=mock_result),
        ):
            u = Updater()
            assert u.apply() is False

    def test_no_venv(self, tmp_path: Path):
        """Should return False when venv doesn't exist."""
        with patch("installer.launcher.updater.VENV_DIR", tmp_path / "novenv"):
            u = Updater()
            assert u.apply() is False
