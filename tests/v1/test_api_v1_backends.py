# Tests for API v1 backends router.
# Created: 2026-02-21

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.backends import router


def _test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


def _client():
    return TestClient(_test_app())


class TestListBackends:
    """Tests for GET /backends."""

    @patch("pocketpaw.api.v1.backends._check_available", return_value=True)
    def test_list_returns_array(self, _mock_check):
        client = _client()
        resp = client.get("/api/v1/backends")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0

    @patch("pocketpaw.api.v1.backends._check_available", return_value=True)
    def test_backend_has_required_fields(self, _mock_check):
        client = _client()
        resp = client.get("/api/v1/backends")
        for backend in resp.json():
            assert "name" in backend
            assert "displayName" in backend
            assert "available" in backend
            assert "capabilities" in backend
            assert isinstance(backend["capabilities"], list)

    @patch("pocketpaw.api.v1.backends._check_available", return_value=False)
    def test_unavailable_backend(self, _mock_check):
        client = _client()
        resp = client.get("/api/v1/backends")
        data = resp.json()
        # At least some backends should show as unavailable
        assert any(not b["available"] for b in data)


class TestCheckAvailable:
    """Tests for the _check_available helper."""

    def test_no_install_hint(self):
        from pocketpaw.api.v1.backends import _check_available

        info = MagicMock()
        info.install_hint = {}
        info.name = "test"
        assert _check_available(info) is True

    def test_missing_import(self):
        from pocketpaw.api.v1.backends import _check_available

        info = MagicMock()
        info.install_hint = {"verify_import": "nonexistent_module_xyz_123"}
        info.name = "test"
        assert _check_available(info) is False


class TestInstallBackend:
    """Tests for POST /backends/install async subprocess behavior."""

    @patch("pocketpaw.api.v1.backends.asyncio.create_subprocess_exec")
    @patch("pocketpaw.agents.registry.get_backend_info")
    def test_install_timeout_kills_process(self, mock_get_backend_info, mock_spawn):
        client = _client()
        mock_get_backend_info.return_value = SimpleNamespace(
            install_hint={"pip_spec": "demo-pkg", "verify_import": "demo_pkg"}
        )

        proc = MagicMock()
        proc.returncode = 0
        proc.kill = MagicMock()
        proc.communicate = AsyncMock(side_effect=[TimeoutError(), (b"", b"")])
        mock_spawn.return_value = proc

        resp = client.post("/api/v1/backends/install", json={"backend": "demo"})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["error"] == "Install failed: timed out while installing demo-pkg"
        proc.kill.assert_called_once()
        assert proc.communicate.await_count == 2

    @patch("pocketpaw.api.v1.backends.asyncio.create_subprocess_exec")
    @patch("pocketpaw.agents.registry.get_backend_info")
    def test_install_error_redacts_secret_stderr(self, mock_get_backend_info, mock_spawn):
        client = _client()
        mock_get_backend_info.return_value = SimpleNamespace(
            install_hint={"pip_spec": "demo-pkg", "verify_import": "demo_pkg"}
        )

        raw_secret = "pp_abcdefghijklmnopqrstuvwx"
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(
            return_value=(b"", f"install failed with key {raw_secret}".encode())
        )
        mock_spawn.return_value = proc

        resp = client.post("/api/v1/backends/install", json={"backend": "demo"})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["error"].startswith("Failed to install demo-pkg:\n")
        assert "[REDACTED]" in payload["error"]
        assert raw_secret not in payload["error"]
