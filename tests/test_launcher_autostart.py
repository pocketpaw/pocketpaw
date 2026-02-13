# Tests for installer/launcher/autostart.py
# Created: 2026-02-11

from __future__ import annotations

import platform
import sys
from unittest.mock import MagicMock, patch

import pytest

# We need to set up the launcher package before importing
sys.modules.setdefault("pystray", MagicMock())
sys.modules.setdefault("PIL", MagicMock())
sys.modules.setdefault("PIL.Image", MagicMock())

from installer.launcher.autostart import AutoStartManager, get_executable_path

# ── get_executable_path ─────────────────────────────────────────────────


class TestGetExecutablePath:
    def test_frozen_returns_sys_executable(self):
        with patch.object(sys, "frozen", True, create=True):
            result = get_executable_path()
            assert result == sys.executable

    def test_source_returns_sys_executable(self):
        # When not frozen, sys.frozen doesn't exist
        if hasattr(sys, "frozen"):
            delattr(sys, "frozen")
        result = get_executable_path()
        assert result == sys.executable


# ── macOS (launchd) ─────────────────────────────────────────────────────


class TestMacOSAutoStart:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.mgr = AutoStartManager()
        self.mgr._system = "Darwin"
        self.plist_path = tmp_path / "LaunchAgents" / "com.pocketpaw.launcher.plist"
        # Patch the plist path
        self._orig = self.mgr._macos_plist_path
        self.mgr._macos_plist_path = lambda: self.plist_path

    def test_is_enabled_false_by_default(self):
        assert not self.mgr.is_enabled()

    def test_enable_creates_plist(self):
        with patch("installer.launcher.autostart.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert self.mgr.enable()
            assert self.plist_path.exists()

    def test_disable_removes_plist(self):
        # Create the plist first
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_text("test")

        with patch("installer.launcher.autostart.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert self.mgr.disable()
            assert not self.plist_path.exists()

    def test_disable_when_not_enabled(self):
        with patch("installer.launcher.autostart.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert self.mgr.disable()

    def test_enable_then_is_enabled(self):
        with patch("installer.launcher.autostart.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            self.mgr.enable()
            assert self.mgr.is_enabled()


# ── Windows (registry) ─────────────────────────────────────────────────


class TestWindowsAutoStart:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mgr = AutoStartManager()
        self.mgr._system = "Windows"

    def test_is_enabled_no_winreg(self):
        # On non-Windows, winreg import fails
        if platform.system() != "Windows":
            assert not self.mgr.is_enabled()

    @patch.dict(sys.modules, {"winreg": MagicMock()})
    def test_enable_sets_registry_key(self):
        import winreg

        mock_key = MagicMock()
        winreg.OpenKey.return_value = mock_key
        winreg.HKEY_CURRENT_USER = 0x80000001
        winreg.KEY_SET_VALUE = 0x0002
        winreg.REG_SZ = 1

        result = self.mgr.enable()
        assert result
        winreg.SetValueEx.assert_called_once()

    @patch.dict(sys.modules, {"winreg": MagicMock()})
    def test_disable_deletes_registry_key(self):
        import winreg

        mock_key = MagicMock()
        winreg.OpenKey.return_value = mock_key
        winreg.HKEY_CURRENT_USER = 0x80000001
        winreg.KEY_SET_VALUE = 0x0002

        result = self.mgr.disable()
        assert result
        winreg.DeleteValue.assert_called_once()


# ── Linux (.desktop file) ──────────────────────────────────────────────


class TestLinuxAutoStart:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.mgr = AutoStartManager()
        self.mgr._system = "Linux"
        self.desktop_path = tmp_path / "autostart" / "pocketpaw.desktop"
        self.mgr._linux_desktop_path = lambda: self.desktop_path

    def test_is_enabled_false_by_default(self):
        assert not self.mgr.is_enabled()

    def test_enable_creates_desktop_file(self):
        assert self.mgr.enable()
        assert self.desktop_path.exists()
        content = self.desktop_path.read_text()
        assert "[Desktop Entry]" in content
        assert "PocketPaw" in content
        assert "Type=Application" in content

    def test_disable_removes_desktop_file(self):
        self.desktop_path.parent.mkdir(parents=True, exist_ok=True)
        self.desktop_path.write_text("test")

        assert self.mgr.disable()
        assert not self.desktop_path.exists()

    def test_disable_when_not_enabled(self):
        assert self.mgr.disable()

    def test_enable_then_is_enabled(self):
        self.mgr.enable()
        assert self.mgr.is_enabled()
