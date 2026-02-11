"""Tests for session index management and REST endpoints.

Created: 2026-02-10
Tests Phase A (session index), Phase B (WS switching), Phase D (recent), Phase E (search).
"""

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pocketclaw.memory.file_store import FileMemoryStore
from pocketclaw.memory.protocol import MemoryEntry, MemoryType

# =========================================================================
# A1: FileMemoryStore session index
# =========================================================================


@pytest.fixture
def store(tmp_path):
    """Create a FileMemoryStore with a temporary directory."""
    return FileMemoryStore(base_path=tmp_path)


@pytest.fixture
def populated_store(store):
    """Store with a few sessions pre-created."""
    sessions = {}
    for i in range(3):
        uid = str(uuid.uuid4())
        safe_key = f"websocket_{uid}"
        session_key = f"websocket:{uid}"
        data = [
            {
                "id": str(uuid.uuid4()),
                "role": "user",
                "content": f"Hello from session {i}",
                "timestamp": (datetime.now() - timedelta(days=2 - i)).isoformat(),
                "metadata": {},
            },
            {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": f"Hi there, session {i} response",
                "timestamp": (datetime.now() - timedelta(days=2 - i, hours=-1)).isoformat(),
                "metadata": {},
            },
        ]
        session_file = store.sessions_path / f"{safe_key}.json"
        session_file.write_text(json.dumps(data, indent=2))
        sessions[safe_key] = {"session_key": session_key, "data": data}

    return store, sessions


class TestSessionIndexPath:
    def test_index_path(self, store):
        assert store._index_path == store.sessions_path / "_index.json"

    def test_index_path_type(self, store):
        assert isinstance(store._index_path, Path)


class TestLoadSaveSessionIndex:
    def test_load_empty(self, store):
        index = store._load_session_index()
        assert index == {}

    def test_save_and_load(self, store):
        index = {"test_123": {"title": "Test", "channel": "websocket"}}
        store._save_session_index(index)
        loaded = store._load_session_index()
        assert loaded == index

    def test_atomic_write(self, store):
        """Verify .tmp file doesn't persist after write."""
        store._save_session_index({"key": {"title": "val"}})
        tmp_file = store._index_path.with_suffix(".tmp")
        assert not tmp_file.exists()
        assert store._index_path.exists()

    def test_load_corrupt_json(self, store):
        store._index_path.write_text("not json {{{")
        index = store._load_session_index()
        assert index == {}


class TestRebuildSessionIndex:
    def test_rebuild_empty(self, store):
        index = store.rebuild_session_index()
        assert index == {}

    def test_rebuild_with_sessions(self, populated_store):
        store, sessions = populated_store
        index = store.rebuild_session_index()
        assert len(index) == 3
        for safe_key in sessions:
            assert safe_key in index
            entry = index[safe_key]
            assert "title" in entry
            assert "channel" in entry
            assert entry["channel"] == "websocket"
            assert entry["message_count"] == 2

    def test_rebuild_skips_index_and_compaction(self, store):
        # Create _index.json and a compaction file — should be skipped
        (store.sessions_path / "_index.json").write_text("{}")
        (store.sessions_path / "test_compaction.json").write_text("{}")
        (store.sessions_path / "websocket_abc.json").write_text(
            json.dumps(
                [{"id": "1", "role": "user", "content": "hi", "timestamp": "2026-01-01T00:00:00"}]
            )
        )
        index = store.rebuild_session_index()
        assert len(index) == 1
        assert "websocket_abc" in index

    def test_rebuild_skips_empty_files(self, store):
        (store.sessions_path / "websocket_empty.json").write_text("[]")
        index = store.rebuild_session_index()
        assert len(index) == 0


class TestUpdateSessionIndex:
    async def test_update_creates_entry(self, store):
        data = [
            {
                "id": "1",
                "role": "user",
                "content": "What is Python?",
                "timestamp": "2026-02-10T10:00:00",
            },
            {
                "id": "2",
                "role": "assistant",
                "content": "Python is a programming language.",
                "timestamp": "2026-02-10T10:01:00",
            },
        ]
        entry = MemoryEntry(
            id="2",
            type=MemoryType.SESSION,
            content="Python is a programming language.",
            role="assistant",
            session_key="websocket:abc123",
        )
        await store._update_session_index("websocket:abc123", entry, data)

        index = store._load_session_index()
        assert "websocket_abc123" in index
        item = index["websocket_abc123"]
        assert item["title"] == "What is Python?"
        assert item["channel"] == "websocket"
        assert item["message_count"] == 2
        assert "Python is a programming language" in item["preview"]

    async def test_update_preserves_user_title(self, store):
        # First write with auto title
        data = [{"id": "1", "role": "user", "content": "Hello", "timestamp": "2026-02-10T10:00:00"}]
        entry = MagicMock(spec=MemoryEntry)
        await store._update_session_index("websocket:test1", entry, data)

        # Rename
        await store.update_session_title("websocket_test1", "My Custom Title")

        # Update again (new message)
        data.append(
            {"id": "2", "role": "assistant", "content": "Hi!", "timestamp": "2026-02-10T10:01:00"}
        )
        await store._update_session_index("websocket:test1", entry, data)

        index = store._load_session_index()
        assert index["websocket_test1"]["title"] == "My Custom Title"


class TestDeleteSession:
    async def test_delete_existing(self, populated_store):
        store, sessions = populated_store
        safe_key = list(sessions.keys())[0]
        session_file = store.sessions_path / f"{safe_key}.json"
        assert session_file.exists()

        # Rebuild index first
        store.rebuild_session_index()
        assert safe_key in store._load_session_index()

        result = await store.delete_session(safe_key)
        assert result is True
        assert not session_file.exists()
        assert safe_key not in store._load_session_index()

    async def test_delete_nonexistent(self, store):
        result = await store.delete_session("nonexistent_session")
        assert result is False

    async def test_delete_removes_compaction(self, store):
        safe_key = "websocket_del123"
        session_file = store.sessions_path / f"{safe_key}.json"
        compaction_file = store.sessions_path / f"{safe_key}_compaction.json"
        session_file.write_text(
            '[{"id":"1","role":"user","content":"hi","timestamp":"2026-01-01"}]'
        )
        compaction_file.write_text('{"watermark":1,"summary":"test"}')

        await store.delete_session(safe_key)
        assert not session_file.exists()
        assert not compaction_file.exists()


class TestUpdateSessionTitle:
    async def test_update_title(self, store):
        store._save_session_index({"websocket_abc": {"title": "Original", "channel": "websocket"}})
        result = await store.update_session_title("websocket_abc", "New Title")
        assert result is True

        index = store._load_session_index()
        assert index["websocket_abc"]["title"] == "New Title"
        assert index["websocket_abc"]["user_title"] == "New Title"

    async def test_update_title_not_found(self, store):
        store._save_session_index({})
        result = await store.update_session_title("nonexistent", "Title")
        assert result is False


class TestSaveSessionEntryIntegration:
    """Test that _save_session_entry updates the index."""

    async def test_save_updates_index(self, store):
        entry = MemoryEntry(
            id="",
            type=MemoryType.SESSION,
            content="Hello world",
            role="user",
            session_key="websocket:integ123",
        )
        await store.save(entry)

        index = store._load_session_index()
        assert "websocket_integ123" in index
        assert index["websocket_integ123"]["message_count"] == 1

    async def test_multiple_saves_update_count(self, store):
        for i, (role, content) in enumerate(
            [("user", "Hi"), ("assistant", "Hello!"), ("user", "How are you?")]
        ):
            entry = MemoryEntry(
                id="",
                type=MemoryType.SESSION,
                content=content,
                role=role,
                session_key="websocket:multi123",
            )
            await store.save(entry)

        index = store._load_session_index()
        assert index["websocket_multi123"]["message_count"] == 3
        assert index["websocket_multi123"]["title"] == "Hi"


class TestIndexMigration:
    """Test that index is built on first run when _index.json doesn't exist."""

    def test_init_builds_index_if_missing(self, tmp_path):
        sessions_path = tmp_path / "sessions"
        sessions_path.mkdir()

        # Pre-populate a session file
        data = [
            {"id": "1", "role": "user", "content": "Test msg", "timestamp": "2026-01-01T00:00:00"}
        ]
        (sessions_path / "websocket_migration.json").write_text(json.dumps(data))

        # Create store — should trigger rebuild
        store = FileMemoryStore(base_path=tmp_path)
        assert (sessions_path / "_index.json").exists()

        index = store._load_session_index()
        assert "websocket_migration" in index


# =========================================================================
# A2: REST Endpoints
# =========================================================================


_TEST_TOKEN = "test-session-token-12345"


@pytest.fixture
def _mock_auth():
    """Mock auth for dashboard API requests."""
    with patch("pocketclaw.dashboard.get_access_token", return_value=_TEST_TOKEN):
        yield


def _auth_headers():
    return {"Authorization": f"Bearer {_TEST_TOKEN}"}


class TestSessionsRESTEndpoints:
    """Test dashboard REST endpoints for sessions."""

    @pytest.fixture
    def client(self, _mock_auth):
        from fastapi.testclient import TestClient

        from pocketclaw.dashboard import app

        return TestClient(app, raise_server_exceptions=False)

    def test_list_sessions(self, client):
        resp = client.get("/api/sessions?limit=5", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert "sessions" in data
        assert "total" in data

    def test_list_sessions_legacy(self, client):
        resp = client.get("/api/memory/sessions?limit=5", headers=_auth_headers())
        assert resp.status_code == 200

    def test_delete_session_not_found(self, client):
        resp = client.delete("/api/sessions/nonexistent_session_12345", headers=_auth_headers())
        assert resp.status_code == 404

    def test_update_title_no_body(self, client):
        resp = client.post(
            "/api/sessions/nonexistent/title",
            json={"title": ""},
            headers=_auth_headers(),
        )
        assert resp.status_code == 400

    def test_update_title_not_found(self, client):
        resp = client.post(
            "/api/sessions/nonexistent_xyz/title",
            json={"title": "My Title"},
            headers=_auth_headers(),
        )
        assert resp.status_code == 404

    def test_search_sessions_empty(self, client):
        resp = client.get("/api/sessions/search?q=", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["sessions"] == []


# =========================================================================
# B1: WebSocket session switching
# =========================================================================


class TestWebSocketSessionSwitching:
    """Test WebSocket switch_session and new_session handlers."""

    @pytest.fixture
    def client(self, _mock_auth):
        from fastapi.testclient import TestClient

        from pocketclaw.dashboard import app

        return TestClient(app, raise_server_exceptions=False)

    def _ws_url(self, extra_params=""):
        base = f"/ws?token={_TEST_TOKEN}"
        return base + ("&" + extra_params if extra_params else "")

    def test_websocket_connect(self, client):
        with client.websocket_connect(self._ws_url()) as ws:
            data = ws.receive_json()
            assert data["type"] == "connection_info"
            assert "id" in data

    def test_websocket_new_session(self, client):
        with client.websocket_connect(self._ws_url()) as ws:
            # Consume connection_info
            ws.receive_json()
            # Send new_session
            ws.send_json({"action": "new_session"})
            data = ws.receive_json()
            assert data["type"] == "new_session"
            assert "id" in data
            assert data["id"].startswith("websocket_")

    def test_websocket_switch_nonexistent_session(self, client):
        with client.websocket_connect(self._ws_url()) as ws:
            ws.receive_json()  # connection_info
            ws.send_json({"action": "switch_session", "session_id": "websocket_nonexistent123"})
            data = ws.receive_json()
            assert data["type"] == "session_history"
            assert data["messages"] == []

    def test_websocket_resume_session(self, client):
        """Test resume_session query parameter."""
        # Create a session file
        sessions_dir = Path.home() / ".pocketclaw" / "memory" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        test_id = str(uuid.uuid4())
        safe_key = f"websocket_{test_id}"
        session_file = sessions_dir / f"{safe_key}.json"

        data = [
            {
                "id": "1",
                "role": "user",
                "content": "Resume test",
                "timestamp": "2026-02-10T10:00:00",
                "metadata": {},
            }
        ]
        session_file.write_text(json.dumps(data))

        try:
            with client.websocket_connect(self._ws_url(f"resume_session={safe_key}")) as ws:
                conn_info = ws.receive_json()
                assert conn_info["type"] == "connection_info"

                # Should receive session_history
                history = ws.receive_json()
                assert history["type"] == "session_history"
                assert history["session_id"] == safe_key
                assert len(history["messages"]) >= 1
        finally:
            if session_file.exists():
                session_file.unlink()
