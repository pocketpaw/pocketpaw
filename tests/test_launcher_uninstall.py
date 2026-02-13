# Tests for installer/launcher/uninstall.py
# Created: 2026-02-11

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("pystray", MagicMock())
sys.modules.setdefault("PIL", MagicMock())
sys.modules.setdefault("PIL.Image", MagicMock())

from installer.launcher.uninstall import Component, Uninstaller


class TestGetComponents:
    def test_returns_list_of_components(self):
        uninstaller = Uninstaller()
        components = uninstaller.get_components()
        assert isinstance(components, list)
        assert all(isinstance(c, Component) for c in components)

    def test_has_expected_component_names(self):
        uninstaller = Uninstaller()
        names = {c.name for c in uninstaller.get_components()}
        assert "venv" in names
        assert "uv" in names
        assert "config" in names
        assert "memory" in names
        assert "logs" in names
        assert "audit" in names
        assert "pid" in names

    def test_component_exists_reflects_filesystem(self, tmp_path):
        with patch("installer.launcher.uninstall.POCKETCLAW_HOME", tmp_path):
            # Create some dirs
            (tmp_path / "venv").mkdir()
            (tmp_path / "logs").mkdir()

            uninstaller = Uninstaller()
            components = {c.name: c for c in uninstaller.get_components()}

            assert components["venv"].exists
            assert components["logs"].exists
            assert not components["config"].exists
            assert not components["memory"].exists


class TestUninstall:
    @pytest.fixture
    def setup_home(self, tmp_path):
        """Create a fake ~/.pocketclaw directory."""
        (tmp_path / "venv" / "bin").mkdir(parents=True)
        (tmp_path / "uv").mkdir()
        (tmp_path / "logs").mkdir()
        (tmp_path / "memory").mkdir()
        (tmp_path / "config.json").write_text("{}")
        (tmp_path / "audit.jsonl").write_text("")
        (tmp_path / "launcher.pid").write_text("12345")
        return tmp_path

    def test_removes_venv_and_uv(self, setup_home):
        with patch("installer.launcher.uninstall.POCKETCLAW_HOME", setup_home):
            uninstaller = Uninstaller()
            results = uninstaller.uninstall(
                remove_venv=True,
                remove_uv=True,
                remove_python=False,
                remove_logs=False,
                remove_config=False,
                remove_memory=False,
            )
            assert not (setup_home / "venv").exists()
            assert not (setup_home / "uv").exists()
            # PID always removed
            assert not (setup_home / "launcher.pid").exists()
            assert any("venv" in r for r in results)

    def test_preserves_config_and_memory_by_default(self, setup_home):
        with patch("installer.launcher.uninstall.POCKETCLAW_HOME", setup_home):
            uninstaller = Uninstaller()
            uninstaller.uninstall()
            assert (setup_home / "config.json").exists()
            assert (setup_home / "memory").exists()

    def test_removes_config_when_requested(self, setup_home):
        with patch("installer.launcher.uninstall.POCKETCLAW_HOME", setup_home):
            uninstaller = Uninstaller()
            uninstaller.uninstall(remove_config=True)
            assert not (setup_home / "config.json").exists()

    def test_removes_memory_when_requested(self, setup_home):
        with patch("installer.launcher.uninstall.POCKETCLAW_HOME", setup_home):
            uninstaller = Uninstaller()
            uninstaller.uninstall(remove_memory=True)
            assert not (setup_home / "memory").exists()

    def test_handles_missing_components(self, tmp_path):
        with patch("installer.launcher.uninstall.POCKETCLAW_HOME", tmp_path):
            uninstaller = Uninstaller()
            results = uninstaller.uninstall()
            # Should not raise, results should mention "not found"
            assert all("not found" in r or "Skipped" in r for r in results)

    def test_disables_autostart(self, setup_home):
        mock_mgr = MagicMock()
        mock_mgr.is_enabled.return_value = True

        with (
            patch("installer.launcher.uninstall.POCKETCLAW_HOME", setup_home),
            patch(
                "installer.launcher.uninstall.Uninstaller.uninstall",
                wraps=Uninstaller().uninstall,
            ),
        ):
            # Test that autostart module is imported and used
            uninstaller = Uninstaller()
            with patch.dict(sys.modules, {"installer.launcher.autostart": MagicMock()}):
                uninstaller.uninstall()

    def test_returns_result_messages(self, setup_home):
        with patch("installer.launcher.uninstall.POCKETCLAW_HOME", setup_home):
            uninstaller = Uninstaller()
            results = uninstaller.uninstall(remove_venv=True, remove_logs=True)
            assert isinstance(results, list)
            assert len(results) > 0
            assert all(isinstance(r, str) for r in results)


class TestInteractiveUninstall:
    def test_no_components_prints_message(self, tmp_path, capsys):
        with patch("installer.launcher.uninstall.POCKETCLAW_HOME", tmp_path):
            uninstaller = Uninstaller()
            uninstaller.interactive_uninstall()
            output = capsys.readouterr().out
            assert "Nothing to remove" in output
