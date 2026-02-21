# Tests for API v1 memory router.
# Created: 2026-02-21

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.memory import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestMemorySettings:
    """Tests for GET/POST /memory/settings."""

    @patch("pocketpaw.config.Settings.load")
    def test_get_memory_settings(self, mock_load, client):
        mock_s = MagicMock(
            memory_backend="file",
            memory_use_inference=False,
            mem0_llm_provider="openai",
            mem0_llm_model="gpt-4o-mini",
            mem0_embedder_provider="openai",
            mem0_embedder_model="text-embedding-3-small",
            mem0_vector_store="qdrant",
            mem0_ollama_base_url="http://localhost:11434",
            mem0_auto_learn=True,
        )
        mock_load.return_value = mock_s
        resp = client.get("/api/v1/memory/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["memory_backend"] == "file"
        assert data["mem0_auto_learn"] is True

    @patch("pocketpaw.memory.get_memory_manager")
    @patch("pocketpaw.config.get_settings")
    @patch("pocketpaw.config.Settings.load")
    def test_save_memory_settings(self, mock_load, mock_get_settings, mock_mgr, client):
        mock_s = MagicMock()
        mock_load.return_value = mock_s
        resp = client.post(
            "/api/v1/memory/settings",
            json={"memory_backend": "mem0", "mem0_auto_learn": True},
        )
        assert resp.status_code == 200
        mock_s.save.assert_called_once()


class TestMemoryLongTerm:
    """Tests for GET/DELETE /memory/long_term."""

    @patch("pocketpaw.memory.get_memory_manager")
    def test_get_long_term_memory(self, mock_get_mgr, client):
        item = MagicMock()
        item.id = "mem-1"
        item.content = "Test memory"
        item.created_at = "2026-02-21T00:00:00"
        item.tags = ["test"]
        store = AsyncMock()
        store.get_by_type.return_value = [item]
        mgr = MagicMock()
        mgr._store = store
        mock_get_mgr.return_value = mgr

        resp = client.get("/api/v1/memory/long_term")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "mem-1"

    @patch("pocketpaw.memory.get_memory_manager")
    def test_get_long_term_memory_with_limit(self, mock_get_mgr, client):
        store = AsyncMock()
        store.get_by_type.return_value = []
        mgr = MagicMock()
        mgr._store = store
        mock_get_mgr.return_value = mgr
        resp = client.get("/api/v1/memory/long_term?limit=10")
        assert resp.status_code == 200
        store.get_by_type.assert_called_once()

    @patch("pocketpaw.memory.get_memory_manager")
    def test_delete_memory_entry(self, mock_get_mgr, client):
        store = AsyncMock()
        store.delete.return_value = True
        mgr = MagicMock()
        mgr._store = store
        mock_get_mgr.return_value = mgr
        resp = client.delete("/api/v1/memory/long_term/mem-1")
        assert resp.status_code == 200

    @patch("pocketpaw.memory.get_memory_manager")
    def test_delete_nonexistent_entry(self, mock_get_mgr, client):
        store = AsyncMock()
        store.delete.return_value = False
        mgr = MagicMock()
        mgr._store = store
        mock_get_mgr.return_value = mgr
        resp = client.delete("/api/v1/memory/long_term/nope")
        assert resp.status_code == 404


class TestMemoryStats:
    """Tests for GET /memory/stats."""

    @patch("pocketpaw.memory.get_memory_manager")
    def test_file_store_stats(self, mock_get_mgr, client):
        store = MagicMock(spec=[])  # No get_memory_stats attr
        mgr = MagicMock()
        mgr._store = store
        mock_get_mgr.return_value = mgr
        resp = client.get("/api/v1/memory/stats")
        assert resp.status_code == 200
        assert resp.json()["backend"] == "file"
