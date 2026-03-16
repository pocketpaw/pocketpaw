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


class TestDownloadFile:
    """Tests for GET /api/v1/files/download."""

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_download_returns_file(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        (tmp_path / "hello.txt").write_text("world")

        resp = client.get(
            "/api/v1/files/download",
            params={"path": str(tmp_path / "hello.txt")},
        )
        assert resp.status_code == 200
        assert resp.text == "world"
        cd = resp.headers.get("content-disposition", "")
        assert "attachment" in cd
        assert "hello.txt" in cd

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_download_nonexistent(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        resp = client.get(
            "/api/v1/files/download",
            params={"path": str(tmp_path / "nope.txt")},
        )
        assert resp.status_code == 404

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=False)
    @patch("pocketpaw.config.get_settings")
    def test_download_path_traversal_rejected(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        resp = client.get(
            "/api/v1/files/download",
            params={"path": "/etc/passwd"},
        )
        assert resp.status_code == 403

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_download_directory_rejected(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        (tmp_path / "subdir").mkdir()

        resp = client.get(
            "/api/v1/files/download",
            params={"path": str(tmp_path / "subdir")},
        )
        assert resp.status_code == 400

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_download_content_disposition_rfc5987(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        name = "café report.txt"
        (tmp_path / name).write_text("data")

        resp = client.get(
            "/api/v1/files/download",
            params={"path": str(tmp_path / name)},
        )
        assert resp.status_code == 200
        cd = resp.headers.get("content-disposition", "")
        assert "filename*=UTF-8''" in cd


class TestDownloadZip:
    """Tests for GET /api/v1/files/download-zip."""

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_zip_returns_archive(self, mock_settings, mock_safe, client, tmp_path):
        import zipfile as zf

        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        d = tmp_path / "project"
        d.mkdir()
        (d / "a.txt").write_text("aaa")
        (d / "b.txt").write_text("bbb")

        resp = client.get(
            "/api/v1/files/download-zip",
            params={"path": str(d)},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

        import io

        buf = io.BytesIO(resp.content)
        with zf.ZipFile(buf) as z:
            names = z.namelist()
            assert "a.txt" in names
            assert "b.txt" in names

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=False)
    @patch("pocketpaw.config.get_settings")
    def test_zip_path_traversal_rejected(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        resp = client.get(
            "/api/v1/files/download-zip",
            params={"path": "/etc"},
        )
        assert resp.status_code == 403

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_zip_not_a_directory(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        (tmp_path / "file.txt").write_text("hi")

        resp = client.get(
            "/api/v1/files/download-zip",
            params={"path": str(tmp_path / "file.txt")},
        )
        assert resp.status_code == 400

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_zip_too_many_files(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        d = tmp_path / "big"
        d.mkdir()
        # We don't actually create 10k files — patch the constant
        with patch("pocketpaw.api.v1.files._ZIP_MAX_FILES", 2):
            for i in range(3):
                (d / f"f{i}.txt").write_text("x")
            resp = client.get(
                "/api/v1/files/download-zip",
                params={"path": str(d)},
            )
        assert resp.status_code == 413
        assert "Too many files" in resp.json()["detail"]

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_zip_cumulative_size_exceeded(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        d = tmp_path / "heavy"
        d.mkdir()
        (d / "a.txt").write_text("data")
        # Cap at 1 byte so the first real file exceeds the limit
        with patch("pocketpaw.api.v1.files._ZIP_MAX_BYTES", 1):
            resp = client.get(
                "/api/v1/files/download-zip",
                params={"path": str(d)},
            )
        assert resp.status_code == 413
        assert "size exceeds" in resp.json()["detail"]


class TestWriteFile:
    """Tests for POST /api/v1/files/write."""

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_write_existing_file(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        target = tmp_path / "edit.txt"
        target.write_text("old")

        resp = client.post(
            "/api/v1/files/write",
            json={"path": str(target), "content": "new"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert target.read_text() == "new"

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_write_nonexistent_file_rejected(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        resp = client.post(
            "/api/v1/files/write",
            json={
                "path": str(tmp_path / "missing.txt"),
                "content": "data",
            },
        )
        assert resp.status_code == 404

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=False)
    @patch("pocketpaw.config.get_settings")
    def test_write_path_traversal_rejected(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        resp = client.post(
            "/api/v1/files/write",
            json={"path": "/etc/passwd", "content": "bad"},
        )
        assert resp.status_code == 403

    @patch("pocketpaw.tools.fetch.is_safe_path", return_value=True)
    @patch("pocketpaw.config.get_settings")
    def test_write_directory_rejected(self, mock_settings, mock_safe, client, tmp_path):
        settings = MagicMock()
        settings.file_jail_path = tmp_path
        mock_settings.return_value = settings

        (tmp_path / "adir").mkdir()

        resp = client.post(
            "/api/v1/files/write",
            json={"path": str(tmp_path / "adir"), "content": "x"},
        )
        assert resp.status_code == 400
