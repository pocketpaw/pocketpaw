# Tests for API v1 skills router.
# Created: 2026-02-21

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.skills import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestListSkills:
    """Tests for GET /skills."""

    @patch("pocketpaw.skills.get_skill_loader")
    def test_list_skills(self, mock_loader_fn, client):
        skill = MagicMock()
        skill.name = "test-skill"
        skill.description = "A test skill"
        skill.argument_hint = "optional arg"
        loader = MagicMock()
        loader.get_invocable.return_value = [skill]
        mock_loader_fn.return_value = loader

        resp = client.get("/api/v1/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "test-skill"

    @patch("pocketpaw.skills.get_skill_loader")
    def test_list_empty_skills(self, mock_loader_fn, client):
        loader = MagicMock()
        loader.get_invocable.return_value = []
        mock_loader_fn.return_value = loader

        resp = client.get("/api/v1/skills")
        assert resp.status_code == 200
        assert resp.json() == []


class TestSearchSkills:
    """Tests for GET /skills/search."""

    def test_search_empty_query(self, client):
        resp = client.get("/api/v1/skills/search?q=")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


class TestInstallSkill:
    """Tests for POST /skills/install."""

    def test_install_missing_source(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": ""})
        assert resp.status_code == 400

    def test_install_path_traversal_blocked(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "../evil/repo"})
        assert resp.status_code == 400

    def test_install_shell_injection_blocked(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "owner/repo;rm -rf"})
        assert resp.status_code == 400

    def test_install_invalid_format(self, client):
        resp = client.post("/api/v1/skills/install", json={"source": "single-part"})
        assert resp.status_code == 400


class TestRemoveSkill:
    """Tests for POST /skills/remove."""

    def test_remove_missing_name(self, client):
        resp = client.post("/api/v1/skills/remove", json={"name": ""})
        assert resp.status_code == 400

    def test_remove_path_traversal_blocked(self, client):
        resp = client.post("/api/v1/skills/remove", json={"name": "../evil"})
        assert resp.status_code == 400

    def test_remove_slash_blocked(self, client):
        resp = client.post("/api/v1/skills/remove", json={"name": "evil/path"})
        assert resp.status_code == 400


class TestReloadSkills:
    """Tests for POST /skills/reload."""

    @patch("pocketpaw.skills.get_skill_loader")
    def test_reload(self, mock_loader_fn, client):
        skill = MagicMock()
        skill.user_invocable = True
        loader = MagicMock()
        loader.reload.return_value = {"test": skill}
        mock_loader_fn.return_value = loader

        resp = client.post("/api/v1/skills/reload")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["count"] == 1
