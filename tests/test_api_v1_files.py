# Tests for API v1 files router.
# Created: 2026-02-20

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.files import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestBrowseFiles:
    """Tests for GET /api/v1/files/browse."""

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_browse_home(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        # Create test files
        (tmp_path / "file.txt").write_text("hello")
        (tmp_path / "subdir").mkdir()

        with patch("pocketpaw.api.v1.files.Path.home", return_value=tmp_path):
            resp = client.get("/api/v1/files/browse", params={"path": "~"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["path"] == "~"
            names = [f["name"] for f in data["files"]]
            assert "subdir" in names
            assert "file.txt" in names

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=False)
    @patch("pocketpaw.config.get_settings")
    def test_browse_access_denied(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        with patch("pocketpaw.api.v1.files.Path.home", return_value=tmp_path):
            resp = client.get("/api/v1/files/browse", params={"path": "/etc/shadow"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["error"] is not None
            assert "access denied" in data["error"].lower()

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_browse_nonexistent(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        with patch("pocketpaw.api.v1.files.Path.home", return_value=tmp_path):
            resp = client.get(
                "/api/v1/files/browse", params={"path": str(tmp_path / "nonexistent")}
            )
            assert resp.status_code == 200
            assert resp.json()["error"] is not None
            assert "not exist" in resp.json()["error"].lower()

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_browse_filters_hidden(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        (tmp_path / ".hidden").mkdir()
        (tmp_path / "visible.txt").write_text("hi")

        with patch("pocketpaw.api.v1.files.Path.home", return_value=tmp_path):
            resp = client.get("/api/v1/files/browse", params={"path": str(tmp_path)})
            assert resp.status_code == 200
            names = [f["name"] for f in resp.json()["files"]]
            assert "visible.txt" in names
            assert ".hidden" not in names

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_browse_includes_sizes(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        (tmp_path / "small.txt").write_text("x" * 100)

        with patch("pocketpaw.api.v1.files.Path.home", return_value=tmp_path):
            resp = client.get("/api/v1/files/browse", params={"path": str(tmp_path)})
            assert resp.status_code == 200
            files = resp.json()["files"]
            txt = [f for f in files if f["name"] == "small.txt"]
            assert len(txt) == 1
            assert "B" in txt[0]["size"]

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_browse_dirs_sorted_first(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        (tmp_path / "zzz_file.txt").write_text("x")
        (tmp_path / "aaa_dir").mkdir()

        with patch("pocketpaw.api.v1.files.Path.home", return_value=tmp_path):
            resp = client.get("/api/v1/files/browse", params={"path": str(tmp_path)})
            assert resp.status_code == 200
            files = resp.json()["files"]
            # Dirs should come first
            assert files[0]["name"] == "aaa_dir"
            assert files[0]["isDir"] is True
