# Tests for API v1 chat router.
# Created: 2026-02-20

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketpaw.api.v1.chat import _APISessionBridge, _active_streams, router


@pytest.fixture
def test_app():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


class TestAPISessionBridge:
    """Tests for the _APISessionBridge class."""

    @pytest.mark.asyncio
    async def test_bridge_creation(self):
        bridge = _APISessionBridge("test-chat-123")
        assert bridge.chat_id == "test-chat-123"
        assert bridge.queue is not None

    @pytest.mark.asyncio
    async def test_bridge_queue_put_get(self):
        bridge = _APISessionBridge("test")
        await bridge.queue.put({"event": "chunk", "data": {"content": "hello"}})
        event = await bridge.queue.get()
        assert event["event"] == "chunk"
        assert event["data"]["content"] == "hello"


class TestChatStream:
    """Tests for POST /api/v1/chat/stream SSE endpoint."""

    @patch("pocketpaw.api.v1.chat._send_message")
    @patch("pocketpaw.api.v1.chat._APISessionBridge")
    def test_stream_returns_sse(self, mock_bridge_cls, mock_send, client):
        # Set up mock bridge
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_send.return_value = "api:test123"

        # Pre-load events into the queue
        async def _load():
            await q.put({"event": "chunk", "data": {"content": "Hello "}})
            await q.put({"event": "chunk", "data": {"content": "world"}})
            await q.put({"event": "stream_end", "data": {"session_id": "api:test123", "usage": {}}})

        asyncio.get_event_loop().run_until_complete(_load())

        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"content": "Hello"},
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            events = list(resp.iter_lines())
            # Should have stream_start, chunks, and stream_end
            event_text = "\n".join(events)
            assert "stream_start" in event_text


class TestChatStop:
    """Tests for POST /api/v1/chat/stop."""

    def test_stop_no_session_id(self, client):
        resp = client.post("/api/v1/chat/stop")
        assert resp.status_code == 400

    def test_stop_nonexistent_session(self, client):
        resp = client.post("/api/v1/chat/stop?session_id=nonexistent")
        assert resp.status_code == 404

    def test_stop_active_stream(self, client):
        event = asyncio.Event()
        _active_streams["test-sess"] = event
        try:
            resp = client.post("/api/v1/chat/stop?session_id=test-sess")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
            assert event.is_set()
        finally:
            _active_streams.pop("test-sess", None)


class TestChatSend:
    """Tests for POST /api/v1/chat (non-streaming)."""

    @patch("pocketpaw.api.v1.chat._send_message")
    @patch("pocketpaw.api.v1.chat._APISessionBridge")
    def test_send_returns_complete_response(self, mock_bridge_cls, mock_send, client):
        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.chat_id = "api:test"
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_send.return_value = "api:test"

        # Load events
        async def _load():
            await q.put({"event": "chunk", "data": {"content": "Hello "}})
            await q.put({"event": "chunk", "data": {"content": "world!"}})
            await q.put({"event": "stream_end", "data": {"session_id": "api:test", "usage": {"tokens": 10}}})

        asyncio.get_event_loop().run_until_complete(_load())

        resp = client.post("/api/v1/chat", json={"content": "Hi"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["content"] == "Hello world!"
        assert data["session_id"] == "api:test"

    def test_send_empty_content(self, client):
        resp = client.post("/api/v1/chat", json={"content": ""})
        assert resp.status_code == 422  # Pydantic validation


class TestSSEFormat:
    """Validate that SSE events follow the spec (event: <type>\\ndata: <json>\\n\\n)."""

    @patch("pocketpaw.api.v1.chat._send_message")
    @patch("pocketpaw.api.v1.chat._APISessionBridge")
    def test_sse_events_are_valid_format(self, mock_bridge_cls, mock_send, client):
        """Each SSE event must have 'event:' and 'data:' lines with valid JSON data."""
        import json as _json

        bridge = MagicMock()
        q = asyncio.Queue()
        bridge.queue = q
        bridge.start = AsyncMock()
        bridge.stop = AsyncMock()
        mock_bridge_cls.return_value = bridge
        mock_send.return_value = "api:sse-test"

        async def _load():
            await q.put({"event": "chunk", "data": {"content": "hi"}})
            await q.put(
                {"event": "stream_end", "data": {"session_id": "api:sse-test", "usage": {}}}
            )

        asyncio.get_event_loop().run_until_complete(_load())

        with client.stream(
            "POST", "/api/v1/chat/stream", json={"content": "test"}
        ) as resp:
            assert resp.status_code == 200
            raw = resp.read().decode()

        # Split SSE events by double newlines
        events = [e.strip() for e in raw.split("\n\n") if e.strip()]
        assert len(events) >= 2, f"Expected at least 2 SSE events, got {len(events)}: {raw!r}"

        for event_block in events:
            lines = event_block.split("\n")
            event_line = next((l for l in lines if l.startswith("event:")), None)
            data_line = next((l for l in lines if l.startswith("data:")), None)

            assert event_line is not None, f"Missing 'event:' line in SSE block: {event_block!r}"
            assert data_line is not None, f"Missing 'data:' line in SSE block: {event_block!r}"

            event_type = event_line.split(":", 1)[1].strip()
            assert event_type, f"Empty event type in: {event_block!r}"

            data_str = data_line.split(":", 1)[1].strip()
            parsed = _json.loads(data_str)
            assert isinstance(parsed, dict), f"SSE data must be a JSON object, got: {type(parsed)}"
