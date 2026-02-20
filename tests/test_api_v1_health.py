# Tests for API v1 health router.
# Created: 2026-02-20

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.health import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestHealthStatus:
    """Tests for GET /api/v1/health."""

    @patch("pocketpaw.health.get_health_engine")
    def test_health_ok(self, mock_engine_fn, client):
        engine = MagicMock()
        engine.summary = {"status": "healthy", "check_count": 5, "issues": []}
        mock_engine_fn.return_value = engine
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"

    @patch("pocketpaw.health.get_health_engine", side_effect=RuntimeError("not init"))
    def test_health_engine_not_available(self, mock_engine_fn, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "unknown"


class TestHealthErrors:
    """Tests for GET /api/v1/health/errors."""

    @patch("pocketpaw.health.get_health_engine")
    def test_get_errors(self, mock_engine_fn, client):
        engine = MagicMock()
        engine.get_recent_errors.return_value = [
            {"timestamp": "2026-02-20", "level": "ERROR", "message": "test"}
        ]
        mock_engine_fn.return_value = engine
        resp = client.get("/api/v1/health/errors")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @patch("pocketpaw.health.get_health_engine")
    def test_get_errors_with_search(self, mock_engine_fn, client):
        engine = MagicMock()
        engine.get_recent_errors.return_value = []
        mock_engine_fn.return_value = engine
        resp = client.get("/api/v1/health/errors?search=test&limit=5")
        assert resp.status_code == 200
        engine.get_recent_errors.assert_called_once_with(limit=5, search="test")


class TestClearHealthErrors:
    """Tests for DELETE /api/v1/health/errors."""

    @patch("pocketpaw.health.get_health_engine")
    def test_clear_errors(self, mock_engine_fn, client):
        engine = MagicMock()
        mock_engine_fn.return_value = engine
        resp = client.delete("/api/v1/health/errors")
        assert resp.status_code == 200
        assert resp.json()["cleared"] is True
        engine.error_store.clear.assert_called_once()


class TestTriggerHealthCheck:
    """Tests for POST /api/v1/health/check."""

    @patch("pocketpaw.health.get_health_engine")
    def test_trigger_check(self, mock_engine_fn, client):
        engine = MagicMock()
        engine.run_all_checks = AsyncMock()
        engine.summary = {"status": "healthy", "check_count": 3, "issues": []}
        mock_engine_fn.return_value = engine
        resp = client.post("/api/v1/health/check")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"


class TestAuditLog:
    """Tests for GET/DELETE /api/v1/audit."""

    @patch("pocketpaw.security.get_audit_logger")
    def test_get_audit_log(self, mock_logger, client):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"action": "login", "timestamp": "2026-02-20"}) + "\n")
            f.write(json.dumps({"action": "logout", "timestamp": "2026-02-20"}) + "\n")
            f.flush()
            mock_logger.return_value.log_path = Path(f.name)
            resp = client.get("/api/v1/audit")
            assert resp.status_code == 200
            logs = resp.json()
            assert len(logs) == 2
            assert logs[0]["action"] == "logout"

    @patch("pocketpaw.security.get_audit_logger")
    def test_get_audit_log_empty(self, mock_logger, client):
        mock_logger.return_value.log_path = Path("/nonexistent/path.jsonl")
        resp = client.get("/api/v1/audit")
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("pocketpaw.security.get_audit_logger")
    def test_clear_audit_log(self, mock_logger, client):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("some data\n")
            f.flush()
            mock_logger.return_value.log_path = Path(f.name)
            resp = client.delete("/api/v1/audit")
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
            assert Path(f.name).read_text() == ""


class TestSecurityAudit:
    """Tests for POST /api/v1/security-audit."""

    def test_security_audit_endpoint(self, client):
        with (
            patch(
                "pocketpaw.security.audit_cli._check_config_permissions",
                return_value=(True, "ok", False),
            ),
            patch(
                "pocketpaw.security.audit_cli._check_plaintext_api_keys",
                return_value=(True, "ok", False),
            ),
            patch(
                "pocketpaw.security.audit_cli._check_audit_log",
                return_value=(True, "ok", False),
            ),
            patch(
                "pocketpaw.security.audit_cli._check_guardian_reachable",
                return_value=(True, "ok", False),
            ),
            patch(
                "pocketpaw.security.audit_cli._check_file_jail",
                return_value=(True, "ok", False),
            ),
            patch(
                "pocketpaw.security.audit_cli._check_tool_profile",
                return_value=(True, "ok", False),
            ),
            patch(
                "pocketpaw.security.audit_cli._check_bypass_permissions",
                return_value=(True, "ok", False),
            ),
        ):
            resp = client.post("/api/v1/security-audit")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 7
            assert data["passed"] == 7
            assert data["issues"] == 0


class TestSelfAuditReports:
    """Tests for /api/v1/self-audit/reports."""

    @patch("pocketpaw.config.get_config_dir")
    def test_list_reports_empty(self, mock_dir, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_dir.return_value = Path(tmpdir)
            resp = client.get("/api/v1/self-audit/reports")
            assert resp.status_code == 200
            assert resp.json() == []

    @patch("pocketpaw.config.get_config_dir")
    def test_list_reports_with_data(self, mock_dir, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            reports = Path(tmpdir) / "audit_reports"
            reports.mkdir()
            (reports / "2026-02-20.json").write_text(
                json.dumps({"total_checks": 10, "passed": 9, "issues": 1})
            )
            mock_dir.return_value = Path(tmpdir)
            resp = client.get("/api/v1/self-audit/reports")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["date"] == "2026-02-20"

    @patch("pocketpaw.config.get_config_dir")
    def test_get_report_by_date(self, mock_dir, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            reports = Path(tmpdir) / "audit_reports"
            reports.mkdir()
            report_data = {"total_checks": 5, "passed": 4, "issues": 1}
            (reports / "2026-02-20.json").write_text(json.dumps(report_data))
            mock_dir.return_value = Path(tmpdir)
            resp = client.get("/api/v1/self-audit/reports/2026-02-20")
            assert resp.status_code == 200
            assert resp.json() == report_data

    @patch("pocketpaw.config.get_config_dir")
    def test_get_report_not_found(self, mock_dir, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_dir.return_value = Path(tmpdir)
            (Path(tmpdir) / "audit_reports").mkdir()
            resp = client.get("/api/v1/self-audit/reports/2026-01-01")
            assert resp.status_code == 404


class TestSelfAuditRun:
    """Tests for POST /api/v1/self-audit/run."""

    @patch("pocketpaw.daemon.self_audit.run_self_audit", new_callable=AsyncMock)
    def test_run_self_audit(self, mock_run, client):
        mock_run.return_value = {"total_checks": 12, "passed": 11, "issues": 1}
        resp = client.post("/api/v1/self-audit/run")
        assert resp.status_code == 200
        assert resp.json()["total_checks"] == 12
