# Tests for installer/launcher/server.py
# Covers: port management, PID lifecycle, health checks, process start/stop.
# Created: 2026-02-10

from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

from installer.launcher.server import ServerManager

# ── Port Management ───────────────────────────────────────────────────


class TestPortManagement:
    """Tests for port detection and free port finding."""

    def test_is_port_free_available(self):
        """Free port should return True."""
        mgr = ServerManager(port=0)
        # Port 0 lets OS pick — find a free one first
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        # Port is now released, should be free
        assert mgr._is_port_free(free_port) is True

    def test_is_port_free_taken(self):
        """Occupied port should return False."""
        mgr = ServerManager()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            taken_port = s.getsockname()[1]
            s.listen(1)
            assert mgr._is_port_free(taken_port) is False

    def test_find_free_port_default_available(self):
        """When default port is free, should return it."""
        mgr = ServerManager(port=49999)
        with patch.object(mgr, "_is_port_free", return_value=True):
            assert mgr._find_free_port() == 49999

    def test_find_free_port_default_taken(self):
        """When default is taken, should find next free port."""
        mgr = ServerManager(port=49999)
        call_count = 0

        def mock_free(port):
            nonlocal call_count
            call_count += 1
            return call_count > 1  # First call (49999) returns False, second (50000) True

        with patch.object(mgr, "_is_port_free", side_effect=mock_free):
            assert mgr._find_free_port() == 50000


# ── Config Reading ────────────────────────────────────────────────────


class TestConfigReading:
    """Tests for reading port from config."""

    def test_read_port_from_config(self, tmp_path: Path):
        """Should read port from config.json."""
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"web_port": 9999}))

        with patch("installer.launcher.server.POCKETCLAW_HOME", tmp_path):
            mgr = ServerManager()
            assert mgr._read_port_from_config() == 9999

    def test_read_port_no_config(self, tmp_path: Path):
        """Should return None when config doesn't exist."""
        with patch("installer.launcher.server.POCKETCLAW_HOME", tmp_path):
            mgr = ServerManager()
            assert mgr._read_port_from_config() is None

    def test_read_port_invalid_json(self, tmp_path: Path):
        """Should return None on invalid JSON."""
        config_path = tmp_path / "config.json"
        config_path.write_text("not json")

        with patch("installer.launcher.server.POCKETCLAW_HOME", tmp_path):
            mgr = ServerManager()
            assert mgr._read_port_from_config() is None

    def test_read_port_no_port_key(self, tmp_path: Path):
        """Should return None when port key is missing."""
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"agent_backend": "claude_agent_sdk"}))

        with patch("installer.launcher.server.POCKETCLAW_HOME", tmp_path):
            mgr = ServerManager()
            assert mgr._read_port_from_config() is None


# ── PID File Management ───────────────────────────────────────────────


class TestPidManagement:
    """Tests for PID file reading and process checks."""

    def test_is_running_no_process_no_pid(self, tmp_path: Path):
        """Should return False when nothing is running."""
        with patch("installer.launcher.server.PID_FILE", tmp_path / "launcher.pid"):
            mgr = ServerManager()
            assert mgr.is_running() is False

    def test_is_running_with_active_process(self):
        """Should return True when managed process is alive."""
        mgr = ServerManager()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        mgr._process = mock_proc
        assert mgr.is_running() is True

    def test_is_running_dead_process(self):
        """Should return False when managed process has exited."""
        mgr = ServerManager()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # Exited
        mgr._process = mock_proc
        assert mgr.is_running() is False

    def test_is_running_stale_pid_file(self, tmp_path: Path):
        """Should clean up stale PID file."""
        pid_file = tmp_path / "launcher.pid"
        pid_file.write_text("99999999")  # Presumably dead PID

        with (
            patch("installer.launcher.server.PID_FILE", pid_file),
            patch.object(ServerManager, "_pid_alive", return_value=False),
        ):
            mgr = ServerManager()
            assert mgr.is_running() is False
            assert not pid_file.exists()  # Cleaned up


# ── Health Check ──────────────────────────────────────────────────────


class TestHealthCheck:
    """Tests for server health check."""

    def test_healthy_server(self):
        """Should return True when server responds 200."""
        mock_resp = MagicMock()
        mock_resp.status = 200

        with patch("urllib.request.urlopen", return_value=mock_resp):
            mgr = ServerManager(port=8888)
            assert mgr.is_healthy() is True

    def test_unhealthy_server(self):
        """Should return False when server doesn't respond."""
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError):
            mgr = ServerManager(port=8888)
            assert mgr.is_healthy() is False

    def test_dashboard_url(self):
        """Should return correct localhost URL."""
        mgr = ServerManager(port=9999)
        assert mgr.get_dashboard_url() == "http://127.0.0.1:9999"


# ── Start/Stop ────────────────────────────────────────────────────────


class TestStartStop:
    """Tests for server start and stop."""

    def test_start_no_python(self, tmp_path: Path):
        """Should fail if venv python doesn't exist."""
        with patch("installer.launcher.server.VENV_DIR", tmp_path / "novenv"):
            mgr = ServerManager()
            status_messages = []
            mgr.on_status = status_messages.append
            assert mgr.start() is False
            assert any("not installed" in m.lower() for m in status_messages)

    def test_start_already_running(self):
        """Should return True without starting again."""
        mgr = ServerManager()
        with patch.object(mgr, "is_running", return_value=True):
            assert mgr.start() is True

    def test_stop_cleans_pid(self, tmp_path: Path):
        """Stop should remove PID file."""
        pid_file = tmp_path / "launcher.pid"
        pid_file.write_text("12345")

        with (
            patch("installer.launcher.server.PID_FILE", pid_file),
            patch.object(ServerManager, "_stop_via_pid"),
        ):
            mgr = ServerManager()
            mgr.stop()
            assert not pid_file.exists()
