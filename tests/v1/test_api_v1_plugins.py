# tests/v1/test_api_v1_plugins.py
# Created: 2026-06-07 (feat/plugin-installer-skills) — API tests for the
# Plugins router: missing source -> 400, PluginInstallError status passthrough,
# and a successful install returning the report. require_scope is bypassed via
# tests/v1/conftest.py's _TESTING_FULL_ACCESS escape hatch.
# Updated: 2026-06-08 (feat/plugin-installer-listremove, #1358) — added tests
# for GET /plugins (list) and POST /plugins/remove (missing name -> 400,
# unknown plugin -> 404, successful remove returns the report).

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.plugins import router
from pocketpaw.plugins.installer import PluginInstallError
from pocketpaw.plugins.models import (
    InstalledPlugin,
    PluginInstallReport,
    PluginInstallStep,
    PluginRemoveReport,
)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


class TestInstallPlugin:
    def test_missing_source(self, client):
        resp = client.post("/api/v1/plugins/install", json={"source": ""})
        assert resp.status_code == 400

    def test_bad_source_maps_to_400(self, client):
        resp = client.post("/api/v1/plugins/install", json={"source": "single"})
        assert resp.status_code == 400

    def test_install_error_status_passthrough(self, client):
        with patch(
            "pocketpaw.api.v1.plugins.PluginInstaller.install",
            new=AsyncMock(side_effect=PluginInstallError("nope", 404)),
        ):
            resp = client.post("/api/v1/plugins/install", json={"source": "acme/widgets"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "nope"

    def test_successful_install_returns_report(self, client):
        report = PluginInstallReport(
            plugin="my-plugin",
            steps=[PluginInstallStep(name="read_manifest", status="succeeded")],
            installed_skills=["alpha"],
        )
        with patch(
            "pocketpaw.api.v1.plugins.PluginInstaller.install",
            new=AsyncMock(return_value=report),
        ):
            resp = client.post("/api/v1/plugins/install", json={"source": "acme/widgets"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["plugin"] == "my-plugin"
        assert data["installed_skills"] == ["alpha"]
        assert data["steps"][0]["name"] == "read_manifest"


class TestListPlugins:
    def test_list_returns_installed_plugins(self, client):
        plugins = [
            InstalledPlugin(
                name="my-plugin",
                version="1.2.3",
                source="acme/widgets",
                skills=["alpha"],
                mcp_servers=["plugin:my-plugin:svc"],
                installed_at="2026-06-08T00:00:00",
            )
        ]
        with patch(
            "pocketpaw.api.v1.plugins.PluginInstaller.list_plugins",
            new=MagicMock(return_value=plugins),
        ):
            resp = client.get("/api/v1/plugins")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "my-plugin"
        assert data[0]["skills"] == ["alpha"]
        assert data[0]["mcp_servers"] == ["plugin:my-plugin:svc"]

    def test_list_empty(self, client):
        with patch(
            "pocketpaw.api.v1.plugins.PluginInstaller.list_plugins",
            new=MagicMock(return_value=[]),
        ):
            resp = client.get("/api/v1/plugins")
        assert resp.status_code == 200
        assert resp.json() == []


class TestRemovePlugin:
    def test_missing_name(self, client):
        resp = client.post("/api/v1/plugins/remove", json={"name": ""})
        assert resp.status_code == 400

    def test_unknown_plugin_maps_to_404(self, client):
        with patch(
            "pocketpaw.api.v1.plugins.PluginInstaller.remove",
            new=AsyncMock(side_effect=PluginInstallError("not installed", 404)),
        ):
            resp = client.post("/api/v1/plugins/remove", json={"name": "ghost"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "not installed"

    def test_successful_remove_returns_report(self, client):
        report = PluginRemoveReport(
            plugin="my-plugin",
            steps=[PluginInstallStep(name="skill:alpha", status="succeeded")],
            removed_skills=["alpha"],
            removed_mcp_servers=["plugin:my-plugin:svc"],
        )
        with patch(
            "pocketpaw.api.v1.plugins.PluginInstaller.remove",
            new=AsyncMock(return_value=report),
        ):
            resp = client.post("/api/v1/plugins/remove", json={"name": "my-plugin"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["plugin"] == "my-plugin"
        assert data["removed_skills"] == ["alpha"]
        assert data["removed_mcp_servers"] == ["plugin:my-plugin:svc"]
