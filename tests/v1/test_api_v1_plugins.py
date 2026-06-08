# tests/v1/test_api_v1_plugins.py
# Created: 2026-06-07 (feat/plugin-installer-skills) — API tests for the
# Plugins router: missing source -> 400, PluginInstallError status passthrough,
# and a successful install returning the report. require_scope is bypassed via
# tests/v1/conftest.py's _TESTING_FULL_ACCESS escape hatch.

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.plugins import router
from pocketpaw.plugins.installer import PluginInstallError
from pocketpaw.plugins.models import PluginInstallReport, PluginInstallStep


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
