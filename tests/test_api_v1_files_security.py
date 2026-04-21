# Files router security tests — verifies scope enforcement and symlink handling.
# Added: 2026-04-16

from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.files import router as files_router


@pytest.fixture
def app_with_scopeless_apikey(tmp_path):
    """Build an app where requests carry an API key with NO scopes.

    Any endpoint that doesn't explicitly require a scope is wide open.
    Any endpoint that does require a scope should return 403.
    """
    app = FastAPI()

    class _Key:
        def __init__(self):
            self.scopes: list[str] = []  # intentionally empty

    @app.middleware("http")
    async def _inject_scopeless(request, call_next):
        request.state.api_key = _Key()
        request.state.oauth_token = None
        return await call_next(request)

    app.include_router(files_router, prefix="/api/v1")
    return app


@pytest.fixture
def client(app_with_scopeless_apikey):
    return TestClient(app_with_scopeless_apikey)


@pytest.fixture
def jailed_settings(tmp_path):
    """Patch settings so file_jail_path is a temp dir we control."""
    jail = tmp_path / "jail"
    jail.mkdir()
    (jail / "alpha.txt").write_text("alpha\n")
    (jail / "beta.txt").write_text("beta\n")

    s = MagicMock()
    s.file_jail_path = jail
    with patch("pocketpaw.api.v1.files.get_settings", return_value=s, create=True):
        with patch("pocketpaw.config.get_settings", return_value=s):
            yield s, jail


class TestScopeEnforcement:
    """Proves #884: any API key (even without scopes) could read arbitrary files."""

    def test_browse_rejects_scopeless_apikey(self, client, jailed_settings):
        _, jail = jailed_settings
        resp = client.get(f"/api/v1/files/browse?path={jail}")
        assert resp.status_code == 403, (
            "GET /files/browse must require files:read — scopeless API key got through"
        )

    def test_content_rejects_scopeless_apikey(self, client, jailed_settings):
        _, jail = jailed_settings
        resp = client.get(f"/api/v1/files/content?path={jail}/alpha.txt")
        assert resp.status_code == 403

    def test_download_rejects_scopeless_apikey(self, client, jailed_settings):
        _, jail = jailed_settings
        resp = client.get(f"/api/v1/files/download?path={jail}/alpha.txt")
        assert resp.status_code == 403

    def test_download_zip_rejects_scopeless_apikey(self, client, jailed_settings):
        _, jail = jailed_settings
        resp = client.get(f"/api/v1/files/download-zip?path={jail}")
        assert resp.status_code == 403

    def test_recent_rejects_scopeless_apikey(self, client, jailed_settings):
        resp = client.get("/api/v1/files/recent?limit=5")
        assert resp.status_code == 403

    def test_open_rejects_scopeless_apikey(self, client, jailed_settings):
        _, jail = jailed_settings
        resp = client.post(
            "/api/v1/files/open",
            json={"path": f"{jail}/alpha.txt", "action": "navigate"},
        )
        assert resp.status_code == 403


class TestSymlinkFilter:
    """Proves #886: /files/download-zip followed symlinks outside the jail."""

    def test_zip_skips_symlink_pointing_outside_jail(self, tmp_path, jailed_settings):
        _, jail = jailed_settings
        secret = tmp_path / "secret.txt"
        secret.write_text("do not leak\n")

        # Place a symlink inside the jail that points outside
        (jail / "link_to_secret.txt").symlink_to(secret)

        app = FastAPI()

        class _AdminKey:
            scopes = ["admin"]

        @app.middleware("http")
        async def _inject(request, call_next):
            request.state.api_key = _AdminKey()
            request.state.oauth_token = None
            return await call_next(request)

        app.include_router(files_router, prefix="/api/v1")
        c = TestClient(app)

        resp = c.get(f"/api/v1/files/download-zip?path={jail}")
        assert resp.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        # Regular files made it in; the symlinked file must not
        assert "alpha.txt" in names
        assert "beta.txt" in names
        assert "link_to_secret.txt" not in names, (
            f"symlink pointing outside jail was packaged in zip: {names}"
        )
