# Tests for Feature 3: Security Audit CLI
# Created: 2026-02-06

import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pocketclaw.security.audit_cli import (
    _check_audit_log,
    _check_bypass_permissions,
    _check_config_permissions,
    _check_file_jail,
    _check_guardian_reachable,
    _check_plaintext_api_keys,
    _check_tool_profile,
    _fix_audit_log,
    _fix_config_permissions,
    run_security_audit,
)


@pytest.fixture
def temp_config_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestConfigPermissions:
    """Tests for config file permission checks."""

    def test_no_config_file(self):
        with patch("pocketclaw.security.audit_cli.get_config_path") as mock:
            mock.return_value = Path("/nonexistent/config.json")
            ok, msg, fixable = _check_config_permissions()
            assert ok is True

    def test_secure_permissions(self, temp_config_dir):
        config = temp_config_dir / "config.json"
        config.write_text("{}")
        os.chmod(config, stat.S_IRUSR | stat.S_IWUSR)

        with patch("pocketclaw.security.audit_cli.get_config_path", return_value=config):
            ok, msg, fixable = _check_config_permissions()
            assert ok is True

    def test_world_readable(self, temp_config_dir):
        config = temp_config_dir / "config.json"
        config.write_text("{}")
        os.chmod(config, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

        with patch("pocketclaw.security.audit_cli.get_config_path", return_value=config):
            ok, msg, fixable = _check_config_permissions()
            assert ok is False
            assert fixable is True

    def test_fix_permissions(self, temp_config_dir):
        config = temp_config_dir / "config.json"
        config.write_text("{}")
        os.chmod(config, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)

        with patch("pocketclaw.security.audit_cli.get_config_path", return_value=config):
            _fix_config_permissions()
            mode = config.stat().st_mode
            assert not (mode & stat.S_IROTH)
            assert not (mode & stat.S_IRGRP)


class TestPlaintextApiKeys:
    """Tests for plaintext API key checks."""

    def test_no_config_file(self):
        with patch("pocketclaw.security.audit_cli.get_config_path") as mock:
            mock.return_value = Path("/nonexistent/config.json")
            ok, msg, fixable = _check_plaintext_api_keys()
            assert ok is True

    def test_no_keys_in_config(self, temp_config_dir):
        config = temp_config_dir / "config.json"
        config.write_text(json.dumps({"agent_backend": "claude_agent_sdk"}))

        with patch("pocketclaw.security.audit_cli.get_config_path", return_value=config):
            ok, msg, fixable = _check_plaintext_api_keys()
            assert ok is True

    def test_keys_in_config(self, temp_config_dir):
        config = temp_config_dir / "config.json"
        config.write_text(json.dumps({"anthropic_api_key": "sk-ant-123"}))

        with patch("pocketclaw.security.audit_cli.get_config_path", return_value=config):
            ok, msg, fixable = _check_plaintext_api_keys()
            assert ok is False
            assert "anthropic_api_key" in msg


class TestAuditLog:
    """Tests for audit log checks."""

    def test_audit_log_missing(self, temp_config_dir):
        with patch("pocketclaw.security.audit_cli.get_config_dir", return_value=temp_config_dir):
            ok, msg, fixable = _check_audit_log()
            assert ok is False
            assert fixable is True

    def test_audit_log_exists(self, temp_config_dir):
        audit = temp_config_dir / "audit.jsonl"
        audit.touch()

        with patch("pocketclaw.security.audit_cli.get_config_dir", return_value=temp_config_dir):
            ok, msg, fixable = _check_audit_log()
            assert ok is True

    def test_fix_creates_audit_log(self, temp_config_dir):
        with patch("pocketclaw.security.audit_cli.get_config_dir", return_value=temp_config_dir):
            _fix_audit_log()
            audit = temp_config_dir / "audit.jsonl"
            assert audit.exists()


class TestGuardianReachable:
    """Tests for guardian agent check."""

    def test_no_api_key(self):
        with patch("pocketclaw.security.audit_cli.get_settings") as mock:
            mock.return_value = MagicMock(anthropic_api_key=None)
            ok, msg, fixable = _check_guardian_reachable()
            assert ok is False

    def test_api_key_set(self):
        with patch("pocketclaw.security.audit_cli.get_settings") as mock:
            mock.return_value = MagicMock(anthropic_api_key="sk-ant-123")
            ok, msg, fixable = _check_guardian_reachable()
            assert ok is True


class TestFileJail:
    """Tests for file jail check."""

    def test_valid_jail(self, temp_config_dir):
        with patch("pocketclaw.security.audit_cli.get_settings") as mock:
            mock.return_value = MagicMock(file_jail_path=temp_config_dir)
            ok, msg, fixable = _check_file_jail()
            assert ok is True

    def test_nonexistent_jail(self):
        with patch("pocketclaw.security.audit_cli.get_settings") as mock:
            mock.return_value = MagicMock(file_jail_path=Path("/nonexistent/path"))
            ok, msg, fixable = _check_file_jail()
            assert ok is False


class TestToolProfile:
    """Tests for tool profile check."""

    def test_full_profile_warns(self):
        with patch("pocketclaw.security.audit_cli.get_settings") as mock:
            mock.return_value = MagicMock(tool_profile="full")
            ok, msg, fixable = _check_tool_profile()
            assert ok is False

    def test_coding_profile_ok(self):
        with patch("pocketclaw.security.audit_cli.get_settings") as mock:
            mock.return_value = MagicMock(tool_profile="coding")
            ok, msg, fixable = _check_tool_profile()
            assert ok is True


class TestBypassPermissions:
    """Tests for bypass permissions check."""

    def test_bypass_enabled_warns(self):
        with patch("pocketclaw.security.audit_cli.get_settings") as mock:
            mock.return_value = MagicMock(bypass_permissions=True)
            ok, msg, fixable = _check_bypass_permissions()
            assert ok is False

    def test_bypass_disabled_ok(self):
        with patch("pocketclaw.security.audit_cli.get_settings") as mock:
            mock.return_value = MagicMock(bypass_permissions=False)
            ok, msg, fixable = _check_bypass_permissions()
            assert ok is True


class TestRunSecurityAudit:
    """Tests for the full audit runner."""

    async def test_all_pass(self):
        with (
            patch(
                "pocketclaw.security.audit_cli._check_config_permissions",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_plaintext_api_keys",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_audit_log",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_guardian_reachable",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_file_jail",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_tool_profile",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_bypass_permissions",
                return_value=(True, "OK", False),
            ),
        ):
            exit_code = await run_security_audit()
            assert exit_code == 0

    async def test_issues_found(self):
        with (
            patch(
                "pocketclaw.security.audit_cli._check_config_permissions",
                return_value=(False, "Bad perms", True),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_plaintext_api_keys",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_audit_log",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_guardian_reachable",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_file_jail",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_tool_profile",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_bypass_permissions",
                return_value=(True, "OK", False),
            ),
        ):
            exit_code = await run_security_audit()
            assert exit_code == 1

    async def test_fix_mode(self):
        fix_called = False

        def mock_fix():
            nonlocal fix_called
            fix_called = True

        with (
            patch(
                "pocketclaw.security.audit_cli._check_config_permissions",
                return_value=(False, "Bad perms", True),
            ),
            patch(
                "pocketclaw.security.audit_cli._fix_config_permissions",
                side_effect=mock_fix,
            ),
            patch(
                "pocketclaw.security.audit_cli._check_plaintext_api_keys",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_audit_log",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_guardian_reachable",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_file_jail",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_tool_profile",
                return_value=(True, "OK", False),
            ),
            patch(
                "pocketclaw.security.audit_cli._check_bypass_permissions",
                return_value=(True, "OK", False),
            ),
        ):
            exit_code = await run_security_audit(fix=True)
            assert exit_code == 0  # Fixed, so 0
            assert fix_called is True
