# Tests for API v1 identity router.
# Created: 2026-02-20

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.identity import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestGetIdentity:
    """Tests for GET /api/v1/identity."""

    @patch("pocketpaw.config.get_config_path")
    @patch("pocketpaw.bootstrap.DefaultBootstrapProvider")
    def test_get_identity(self, mock_provider_cls, mock_config_path, client):
        mock_config_path.return_value = Path("/tmp/test/config.json")
        context = MagicMock()
        context.identity = "I am PocketPaw"
        context.soul = "Helpful assistant"
        context.style = "Concise"
        context.instructions = "Follow rules"
        context.user_profile = "User info"
        mock_provider_cls.return_value.get_context = AsyncMock(return_value=context)

        resp = client.get("/api/v1/identity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["identity_file"] == "I am PocketPaw"
        assert data["soul_file"] == "Helpful assistant"
        assert data["style_file"] == "Concise"
        assert data["instructions_file"] == "Follow rules"
        assert data["user_file"] == "User info"


class TestSaveIdentity:
    """Tests for PUT /api/v1/identity."""

    @patch("pocketpaw.config.get_config_path")
    def test_save_identity_files(self, mock_config_path, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_path.return_value = Path(tmpdir) / "config.json"
            resp = client.put(
                "/api/v1/identity",
                json={
                    "identity_file": "Updated identity",
                    "soul_file": "Updated soul",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert "IDENTITY.md" in data["updated"]
            assert "SOUL.md" in data["updated"]
            identity_dir = Path(tmpdir) / "identity"
            assert (identity_dir / "IDENTITY.md").read_text() == "Updated identity"
            assert (identity_dir / "SOUL.md").read_text() == "Updated soul"

    @patch("pocketpaw.config.get_config_path")
    def test_save_partial_update(self, mock_config_path, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_path.return_value = Path(tmpdir) / "config.json"
            resp = client.put("/api/v1/identity", json={"style_file": "New style"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["updated"] == ["STYLE.md"]

    @patch("pocketpaw.config.get_config_path")
    def test_save_empty_body(self, mock_config_path, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config_path.return_value = Path(tmpdir) / "config.json"
            resp = client.put("/api/v1/identity", json={})
            assert resp.status_code == 200
            assert resp.json()["updated"] == []
