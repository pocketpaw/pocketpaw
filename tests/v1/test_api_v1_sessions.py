# Tests for API v1 sessions router.
# Created: 2026-02-20

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.sessions import router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


def _make_store_with_index(index: dict, sessions_path: Path | None = None):
    store = MagicMock()
    store._load_session_index.return_value = index
    if sessions_path:
        store.sessions_path = sessions_path
    return store


class TestListSessions:
    """Tests for GET /api/v1/sessions."""

    @patch("pocketpaw.memory.get_memory_manager")
    def test_list_sessions_empty(self, mock_mgr, client):
        store = _make_store_with_index({})
        mock_mgr.return_value._store = store
        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sessions"] == []
        assert data["total"] == 0

    @patch("pocketpaw.memory.get_memory_manager")
    def test_list_sessions_with_data(self, mock_mgr, client):
        index = {
            "sess1": {
                "title": "Chat 1",
                "channel": "websocket",
                "last_activity": "2026-02-20T10:00:00",
                "message_count": 5,
            },
            "sess2": {
                "title": "Chat 2",
                "channel": "telegram",
                "last_activity": "2026-02-20T11:00:00",
                "message_count": 3,
            },
        }
        store = _make_store_with_index(index)
        mock_mgr.return_value._store = store
        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["sessions"]) == 2
        assert data["sessions"][0]["id"] == "sess2"

    @patch("pocketpaw.memory.get_memory_manager")
    def test_list_sessions_with_limit(self, mock_mgr, client):
        index = {f"s{i}": {"last_activity": f"2026-02-20T{i:02d}:00:00"} for i in range(10)}
        store = _make_store_with_index(index)
        mock_mgr.return_value._store = store
        resp = client.get("/api/v1/sessions?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()["sessions"]) == 3

    @patch("pocketpaw.memory.get_memory_manager")
    def test_list_sessions_no_index(self, mock_mgr, client):
        store = MagicMock(spec=[])
        mock_mgr.return_value._store = store
        resp = client.get("/api/v1/sessions")
        assert resp.status_code == 200
        assert resp.json() == {"sessions": [], "total": 0}


class TestDeleteSession:
    """Tests for DELETE /api/v1/sessions/{session_id}."""

    @patch("pocketpaw.memory.get_memory_manager")
    def test_delete_existing(self, mock_mgr, client):
        store = MagicMock()
        store.delete_session = AsyncMock(return_value=True)
        mock_mgr.return_value._store = store
        resp = client.delete("/api/v1/sessions/sess1")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @patch("pocketpaw.memory.get_memory_manager")
    def test_delete_not_found(self, mock_mgr, client):
        store = MagicMock()
        store.delete_session = AsyncMock(return_value=False)
        mock_mgr.return_value._store = store
        resp = client.delete("/api/v1/sessions/nonexistent")
        assert resp.status_code == 404

    @patch("pocketpaw.memory.get_memory_manager")
    def test_delete_unsupported_store(self, mock_mgr, client):
        store = MagicMock(spec=[])
        mock_mgr.return_value._store = store
        resp = client.delete("/api/v1/sessions/sess1")
        assert resp.status_code == 501


class TestUpdateTitle:
    """Tests for POST /api/v1/sessions/{session_id}/title."""

    @patch("pocketpaw.memory.get_memory_manager")
    def test_update_title(self, mock_mgr, client):
        store = MagicMock()
        store.update_session_title = AsyncMock(return_value=True)
        mock_mgr.return_value._store = store
        resp = client.post("/api/v1/sessions/sess1/title", json={"title": "New Title"})
        assert resp.status_code == 200

    @patch("pocketpaw.memory.get_memory_manager")
    def test_update_title_not_found(self, mock_mgr, client):
        store = MagicMock()
        store.update_session_title = AsyncMock(return_value=False)
        mock_mgr.return_value._store = store
        resp = client.post("/api/v1/sessions/sess1/title", json={"title": "New"})
        assert resp.status_code == 404

    def test_update_title_empty(self, client):
        resp = client.post("/api/v1/sessions/sess1/title", json={"title": ""})
        assert resp.status_code == 422


class TestSearchSessions:
    """Tests for GET /api/v1/sessions/search."""

    def test_search_empty_query(self, client):
        resp = client.get("/api/v1/sessions/search?q=")
        assert resp.status_code == 200
        assert resp.json()["sessions"] == []

    @patch("pocketpaw.memory.get_memory_manager")
    def test_search_with_matches(self, mock_mgr, client):
        with tempfile.TemporaryDirectory() as tmpdir:
            sessions_path = Path(tmpdir)
            (sessions_path / "sess1.json").write_text(
                json.dumps([{"content": "Hello world", "role": "user"}])
            )
            (sessions_path / "sess2.json").write_text(
                json.dumps([{"content": "Goodbye world", "role": "assistant"}])
            )
            store = MagicMock()
            store.sessions_path = sessions_path
            store._load_session_index.return_value = {
                "sess1": {"title": "Chat 1", "channel": "ws", "last_activity": ""},
                "sess2": {"title": "Chat 2", "channel": "ws", "last_activity": ""},
            }
            mock_mgr.return_value._store = store
            resp = client.get("/api/v1/sessions/search?q=hello")
            assert resp.status_code == 200
            results = resp.json()["sessions"]
            assert len(results) == 1
            assert results[0]["id"] == "sess1"

    @patch("pocketpaw.memory.get_memory_manager")
    def test_search_no_sessions_path(self, mock_mgr, client):
        store = MagicMock(spec=[])
        mock_mgr.return_value._store = store
        resp = client.get("/api/v1/sessions/search?q=hello")
        assert resp.status_code == 200
        assert resp.json()["sessions"] == []
