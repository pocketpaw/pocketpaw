# Tests for daemon/self_audit.py
# Created: 2026-02-07

from unittest.mock import MagicMock, patch

from pocketclaw.daemon.self_audit import (
    _check_audit_log_size,
    _check_config_conflicts,
    _check_disk_usage,
    _check_orphan_oauth_tokens,
    _check_stale_sessions,
    run_self_audit,
)

# ---------------------------------------------------------------------------
# _check_stale_sessions
# ---------------------------------------------------------------------------


class TestStaleSessions:
    def test_no_sessions_dir(self, tmp_path):
        with patch("pocketclaw.daemon.self_audit.get_config_dir", return_value=tmp_path):
            ok, msg = _check_stale_sessions()
            assert ok is True
            assert "No sessions" in msg

    def test_no_stale_sessions(self, tmp_path):
        sessions = tmp_path / "memory" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "recent.json").write_text("{}")
        with patch("pocketclaw.daemon.self_audit.get_config_dir", return_value=tmp_path):
            ok, msg = _check_stale_sessions()
            assert ok is True


# ---------------------------------------------------------------------------
# _check_config_conflicts
# ---------------------------------------------------------------------------


class TestConfigConflicts:
    def test_no_conflicts(self):
        mock_settings = MagicMock()
        mock_settings.bypass_permissions = False
        mock_settings.plan_mode = False
        mock_settings.injection_scan_enabled = True
        with patch("pocketclaw.daemon.self_audit.get_settings", return_value=mock_settings):
            ok, msg = _check_config_conflicts()
            assert ok is True

    def test_bypass_with_plan_mode(self):
        mock_settings = MagicMock()
        mock_settings.bypass_permissions = True
        mock_settings.plan_mode = True
        mock_settings.injection_scan_enabled = True
        with patch("pocketclaw.daemon.self_audit.get_settings", return_value=mock_settings):
            ok, msg = _check_config_conflicts()
            assert ok is False
            assert "bypass_permissions" in msg

    def test_no_safety_net(self):
        mock_settings = MagicMock()
        mock_settings.bypass_permissions = False
        mock_settings.plan_mode = False
        mock_settings.injection_scan_enabled = False
        with patch("pocketclaw.daemon.self_audit.get_settings", return_value=mock_settings):
            ok, msg = _check_config_conflicts()
            assert ok is False
            assert "safety net" in msg


# ---------------------------------------------------------------------------
# _check_disk_usage
# ---------------------------------------------------------------------------


class TestDiskUsage:
    def test_small_directory(self, tmp_path):
        (tmp_path / "test.txt").write_text("hello")
        with patch("pocketclaw.daemon.self_audit.get_config_dir", return_value=tmp_path):
            ok, msg = _check_disk_usage()
            assert ok is True
            assert "MB" in msg


# ---------------------------------------------------------------------------
# _check_audit_log_size
# ---------------------------------------------------------------------------


class TestAuditLogSize:
    def test_no_audit_log(self, tmp_path):
        with patch("pocketclaw.daemon.self_audit.get_config_dir", return_value=tmp_path):
            ok, msg = _check_audit_log_size()
            assert ok is True

    def test_small_audit_log(self, tmp_path):
        (tmp_path / "audit.jsonl").write_text("{}\n" * 100)
        with patch("pocketclaw.daemon.self_audit.get_config_dir", return_value=tmp_path):
            ok, msg = _check_audit_log_size()
            assert ok is True


# ---------------------------------------------------------------------------
# _check_orphan_oauth_tokens
# ---------------------------------------------------------------------------


class TestOAuthTokens:
    def test_no_oauth_dir(self, tmp_path):
        with patch("pocketclaw.daemon.self_audit.get_config_dir", return_value=tmp_path):
            ok, msg = _check_orphan_oauth_tokens()
            assert ok is True

    def test_with_tokens(self, tmp_path):
        oauth_dir = tmp_path / "oauth"
        oauth_dir.mkdir()
        (oauth_dir / "google_gmail.json").write_text("{}")
        with patch("pocketclaw.daemon.self_audit.get_config_dir", return_value=tmp_path):
            ok, msg = _check_orphan_oauth_tokens()
            assert ok is True
            assert "1 OAuth" in msg


# ---------------------------------------------------------------------------
# run_self_audit (integration)
# ---------------------------------------------------------------------------


async def test_run_self_audit(tmp_path):
    """Full audit should run without crashing."""
    mock_settings = MagicMock()
    mock_settings.bypass_permissions = False
    mock_settings.plan_mode = False
    mock_settings.injection_scan_enabled = True
    mock_settings.tool_profile = "coding"
    mock_settings.anthropic_api_key = "test-key"
    mock_settings.file_jail_path = tmp_path

    config_dir = tmp_path / ".pocketclaw"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("{}")
    (config_dir / "audit.jsonl").write_text("")

    with (
        patch("pocketclaw.daemon.self_audit.get_config_dir", return_value=config_dir),
        patch("pocketclaw.daemon.self_audit.get_settings", return_value=mock_settings),
        patch("pocketclaw.security.audit_cli.get_config_dir", return_value=config_dir),
        patch(
            "pocketclaw.security.audit_cli.get_config_path",
            return_value=config_dir / "config.json",
        ),
        patch("pocketclaw.security.audit_cli.get_settings", return_value=mock_settings),
    ):
        report = await run_self_audit()
        assert "total_checks" in report
        assert "results" in report
        assert report["total_checks"] > 0
