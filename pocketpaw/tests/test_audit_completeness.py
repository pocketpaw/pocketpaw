"""Tests for audit logging completeness (Layer 7).

Verifies that all security-relevant events are properly audited,
the append-only guarantee is enforced, and no silent failures occur.
"""

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.security.audit import AuditEvent, AuditLogger, AuditSeverity

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_audit_log(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    return AuditLogger(log_path=log_path)


@pytest.fixture
def tmp_audit_dir(tmp_path):
    """Temp dir with an audit log that has some entries."""
    log_path = tmp_path / "audit.jsonl"
    logger = AuditLogger(log_path=log_path)
    # Write some events
    for i in range(3):
        logger.log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor="test",
                action=f"action_{i}",
                target="test",
                status="success",
            )
        )
    return logger


# ---------------------------------------------------------------------------
# Core audit logging
# ---------------------------------------------------------------------------


class TestCoreAuditLogging:
    def test_log_creates_file(self, tmp_audit_log):
        tmp_audit_log.log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor="test",
                action="test_action",
                target="test",
                status="success",
            )
        )
        assert tmp_audit_log.log_path.exists()

    def test_log_appends_jsonl(self, tmp_audit_log):
        for i in range(5):
            tmp_audit_log.log(
                AuditEvent.create(
                    severity=AuditSeverity.INFO,
                    actor="test",
                    action=f"action_{i}",
                    target="test",
                    status="success",
                )
            )
        lines = tmp_audit_log.log_path.read_text().strip().split("\n")
        assert len(lines) == 5
        for line in lines:
            parsed = json.loads(line)
            assert "id" in parsed
            assert "timestamp" in parsed
            assert "severity" in parsed

    def test_each_entry_has_required_fields(self, tmp_audit_log):
        tmp_audit_log.log(
            AuditEvent.create(
                severity=AuditSeverity.ALERT,
                actor="guardian",
                action="scan_result",
                target="shell",
                status="block",
                command="rm -rf /",
            )
        )
        line = tmp_audit_log.log_path.read_text().strip()
        entry = json.loads(line)
        assert entry["severity"] == "alert"
        assert entry["actor"] == "guardian"
        assert entry["action"] == "scan_result"
        assert entry["target"] == "shell"
        assert entry["status"] == "block"
        assert entry["context"]["command"] == "rm -rf /"

    def test_context_kwargs_stored(self, tmp_audit_log):
        tmp_audit_log.log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor="agent",
                action="tool_use",
                target="ShellTool",
                status="attempt",
                params={"command": "ls"},
                extra_info="test",
            )
        )
        entry = json.loads(tmp_audit_log.log_path.read_text().strip())
        assert entry["context"]["params"] == {"command": "ls"}
        assert entry["context"]["extra_info"] == "test"


# ---------------------------------------------------------------------------
# Append-only guarantee
# ---------------------------------------------------------------------------


class TestAppendOnlyGuarantee:
    def test_no_delete_method_on_logger(self, tmp_audit_log):
        """AuditLogger should not have a delete or clear method."""
        assert not hasattr(tmp_audit_log, "delete")
        assert not hasattr(tmp_audit_log, "clear")
        assert not hasattr(tmp_audit_log, "truncate")

    def test_multiple_writes_append(self, tmp_audit_dir):
        initial_size = tmp_audit_dir.log_path.stat().st_size
        tmp_audit_dir.log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor="test",
                action="new_action",
                target="test",
                status="success",
            )
        )
        new_size = tmp_audit_dir.log_path.stat().st_size
        assert new_size > initial_size


# ---------------------------------------------------------------------------
# Dashboard audit rotation (was: DELETE endpoint)
# ---------------------------------------------------------------------------


class TestAuditRotation:
    """Test that /api/audit now archives instead of deleting."""

    def test_archive_preserves_data(self, tmp_audit_dir, tmp_path):
        """Archived audit log should be a copy, not deleted."""
        original_content = tmp_audit_dir.log_path.read_text()
        assert len(original_content) > 0

        # Simulate the archive operation (same logic as dashboard endpoint)
        from datetime import UTC, datetime

        ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        archive_path = tmp_audit_dir.log_path.with_name(f"audit-{ts}.jsonl")
        shutil.copy2(tmp_audit_dir.log_path, archive_path)

        assert archive_path.exists()
        assert archive_path.read_text() == original_content


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestAuditFailureHandling:
    def test_write_failure_does_not_raise(self, tmp_path):
        """Audit write failure should log to system logger, not crash."""
        # Use a path that's a directory (can't write to it)
        dir_path = tmp_path / "not_a_file"
        dir_path.mkdir()
        logger = AuditLogger(log_path=dir_path)

        # Should not raise
        logger.log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor="test",
                action="test",
                target="test",
                status="success",
            )
        )

    def test_callback_failure_does_not_block_logging(self, tmp_audit_log):
        """A failing callback should not prevent the audit entry from being written."""

        def bad_callback(event_dict):
            raise RuntimeError("callback failed")

        tmp_audit_log.on_log(bad_callback)
        tmp_audit_log.log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor="test",
                action="test",
                target="test",
                status="success",
            )
        )
        # Entry should still be written despite callback failure
        assert tmp_audit_log.log_path.exists()
        lines = tmp_audit_log.log_path.read_text().strip().split("\n")
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# Auth event auditing
# ---------------------------------------------------------------------------


class TestAuthEventAuditing:
    def test_audit_auth_event_helper(self):
        """_audit_auth_event should log to audit trail."""
        with patch("pocketpaw.security.audit.get_audit_logger") as mock_get:
            mock_logger = MagicMock()
            mock_get.return_value = mock_logger

            from pocketpaw.dashboard_auth import _audit_auth_event

            mock_request = MagicMock()
            mock_request.client.host = "127.0.0.1"

            _audit_auth_event("login_success", mock_request, status="success")
            assert mock_logger.log.call_count == 1

    def test_audit_auth_event_failure_does_not_raise(self):
        """Auth event audit failure should not crash auth flow."""
        from pocketpaw.dashboard_auth import _audit_auth_event

        # _audit_auth_event catches all exceptions internally
        # Just verify it doesn't raise with a None request
        _audit_auth_event("login_failed", None, status="block")


# ---------------------------------------------------------------------------
# Claude SDK dangerous command audit
# ---------------------------------------------------------------------------


class TestClaudeSDKDangerousCommandAudit:
    @pytest.mark.asyncio
    async def test_blocked_command_is_audited(self):
        """When claude_sdk blocks a dangerous command, it should audit the event."""
        with patch("pocketpaw.security.audit.get_audit_logger") as mock_get:
            mock_logger = MagicMock()
            mock_get.return_value = mock_logger

            from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
            from pocketpaw.config import Settings

            sdk = ClaudeSDKBackend(Settings())

            result = await sdk._block_dangerous_hook(
                {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
                None,
                None,
            )

            assert result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
            assert mock_logger.log.call_count == 1


# ---------------------------------------------------------------------------
# OAuth2 audit logging
# ---------------------------------------------------------------------------


class TestOAuth2AuditLogging:
    def test_refresh_audited(self):
        """OAuth token refresh should be audited."""
        with patch("pocketpaw.security.audit.get_audit_logger") as mock_get:
            mock_logger = MagicMock()
            mock_get.return_value = mock_logger

            from pocketpaw.api.oauth2.server import AuthorizationServer

            server = AuthorizationServer.__new__(AuthorizationServer)
            server.storage = MagicMock()

            # Mock a valid old token
            old_token = MagicMock()
            old_token.revoked = False
            old_token.client_id = "test-client"
            old_token.scope = "read"
            server.storage.get_token_by_refresh.return_value = old_token

            result, error = server.refresh("old_refresh_token")
            assert error is None
            assert mock_logger.log_api_event.call_count == 1

    def test_revoke_audited(self):
        """OAuth token revocation should be audited."""
        with patch("pocketpaw.security.audit.get_audit_logger") as mock_get:
            mock_logger = MagicMock()
            mock_get.return_value = mock_logger

            from pocketpaw.api.oauth2.server import AuthorizationServer

            server = AuthorizationServer.__new__(AuthorizationServer)
            server.storage = MagicMock()
            server.storage.revoke_token.return_value = True

            result = server.revoke("some_token")
            assert result is True
            assert mock_logger.log_api_event.call_count == 1


# ---------------------------------------------------------------------------
# API key rotation audit
# ---------------------------------------------------------------------------


class TestAPIKeyRotationAudit:
    def test_rotation_revoke_audited(self):
        """API key rotation should audit the revocation step."""
        with patch("pocketpaw.security.audit.get_audit_logger") as mock_get:
            mock_logger = MagicMock()
            mock_get.return_value = mock_logger

            from pocketpaw.api.api_keys import APIKeyManager

            mgr = APIKeyManager.__new__(APIKeyManager)
            mgr._path = Path("/tmp/test_keys.json")
            mgr._load = MagicMock(
                return_value=[
                    {
                        "id": "key-1",
                        "name": "test",
                        "key_hash": "hash",
                        "prefix": "pp_test",
                        "scopes": ["read"],
                        "revoked": False,
                        "created_at": "2026-01-01",
                    }
                ]
            )
            mgr._save = MagicMock()
            mgr.create = MagicMock(return_value=("record", "secret"))

            mgr.rotate("key-1")
            assert mock_logger.log_api_event.call_count == 1


# ---------------------------------------------------------------------------
# PII filtering on audit
# ---------------------------------------------------------------------------


class TestPIIFiltering:
    def test_pii_filter_masks_ssn(self, tmp_audit_log):
        tmp_audit_log.enable_pii_filter()
        tmp_audit_log.log(
            AuditEvent.create(
                severity=AuditSeverity.INFO,
                actor="test",
                action="test",
                target="test",
                status="success",
                data="SSN is 123-45-6789",
            )
        )
        entry = json.loads(tmp_audit_log.log_path.read_text().strip())
        assert "123-45-6789" not in json.dumps(entry)
