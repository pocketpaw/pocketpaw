"""Tests for Matrix Channel Adapter — Sprint 21.

matrix-nio is mocked since it's an optional dependency.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Mock nio before importing the adapter
mock_nio = MagicMock()
mock_nio.AsyncClient = MagicMock
mock_nio.RoomMessageText = type("RoomMessageText", (), {})
mock_nio.RoomSendResponse = type("RoomSendResponse", (), {"event_id": "evt1"})
sys.modules.setdefault("nio", mock_nio)


from pocketclaw.bus.adapters.matrix_adapter import MatrixAdapter  # noqa: E402
from pocketclaw.bus.events import Channel, OutboundMessage  # noqa: E402


class TestMatrixAdapterInit:
    def test_defaults(self):
        adapter = MatrixAdapter()
        assert adapter.homeserver == ""
        assert adapter.user_id == ""
        assert adapter.channel == Channel.MATRIX
        assert adapter.device_id == "POCKETPAW"

    def test_custom_config(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
            access_token="tok123",
            allowed_room_ids=["!room:matrix.org"],
        )
        assert adapter.homeserver == "https://matrix.org"
        assert adapter.user_id == "@bot:matrix.org"
        assert adapter.access_token == "tok123"
        assert adapter.allowed_room_ids == ["!room:matrix.org"]


class TestMatrixAdapterMessage:
    async def test_handle_valid_message(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        room = SimpleNamespace(room_id="!room:matrix.org", display_name="TestRoom")
        event = SimpleNamespace(
            sender="@user:matrix.org",
            body="Hello Matrix!",
            event_id="$event1",
        )

        await adapter._on_message(room, event)

        adapter._bus.publish_inbound.assert_called_once()
        call_args = adapter._bus.publish_inbound.call_args[0][0]
        assert call_args.content == "Hello Matrix!"
        assert call_args.sender_id == "@user:matrix.org"
        assert call_args.chat_id == "!room:matrix.org"
        assert call_args.channel == Channel.MATRIX

    async def test_skip_own_messages(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        room = SimpleNamespace(room_id="!room:matrix.org")
        event = SimpleNamespace(
            sender="@bot:matrix.org",  # own message
            body="echo",
            event_id="$evt",
        )

        await adapter._on_message(room, event)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_unauthorized_room_filtered(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
            allowed_room_ids=["!allowed:matrix.org"],
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        room = SimpleNamespace(room_id="!other:matrix.org")
        event = SimpleNamespace(
            sender="@user:matrix.org",
            body="blocked",
            event_id="$evt",
        )

        await adapter._on_message(room, event)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_empty_message_skipped(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        room = SimpleNamespace(room_id="!room:matrix.org")
        event = SimpleNamespace(sender="@user:matrix.org", body="", event_id="$evt")

        await adapter._on_message(room, event)
        adapter._bus.publish_inbound.assert_not_called()


class TestMatrixAdapterSend:
    async def test_send_normal_message(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        mock_client = AsyncMock()

        # Create a proper mock response class
        class FakeRoomSendResponse:
            event_id = "$sent1"

        resp = FakeRoomSendResponse()
        mock_client.room_send = AsyncMock(return_value=resp)
        adapter._client = mock_client

        # Patch nio.RoomSendResponse so isinstance check works
        mock_nio.RoomSendResponse = FakeRoomSendResponse

        msg = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!room:matrix.org",
            content="Hello!",
        )
        await adapter.send(msg)
        mock_client.room_send.assert_called_once()

    async def test_send_stream_accumulates(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        mock_client = AsyncMock()
        adapter._client = mock_client

        # Send chunks
        chunk1 = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!r",
            content="Hello ",
            is_stream_chunk=True,
        )
        chunk2 = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!r",
            content="World!",
            is_stream_chunk=True,
        )

        await adapter.send(chunk1)
        await adapter.send(chunk2)

        assert adapter._buffers.get("!r") == "Hello World!"

    async def test_send_stream_end_flushes(self):
        adapter = MatrixAdapter(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
        )
        mock_client = AsyncMock()
        resp = SimpleNamespace(event_id="$sent")
        mock_client.room_send = AsyncMock(return_value=resp)
        adapter._client = mock_client

        adapter._buffers["!r"] = "accumulated text"

        end = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!r",
            content="",
            is_stream_end=True,
        )
        await adapter.send(end)
        mock_client.room_send.assert_called_once()

    async def test_send_empty_skipped(self):
        adapter = MatrixAdapter()
        mock_client = AsyncMock()
        adapter._client = mock_client

        msg = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!r",
            content="   ",
        )
        await adapter.send(msg)
        mock_client.room_send.assert_not_called()

    async def test_send_without_client(self):
        adapter = MatrixAdapter()
        # _client is None
        msg = OutboundMessage(
            channel=Channel.MATRIX,
            chat_id="!r",
            content="test",
        )
        await adapter.send(msg)  # should not raise
