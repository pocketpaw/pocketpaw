# Tests for API v1 backends router.
# Created: 2026-02-21

from unittest.mock import MagicMock, patch

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
