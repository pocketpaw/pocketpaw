"""Tests for Microsoft Teams Channel Adapter — Sprint 22.

botbuilder-core is mocked since it's an optional dependency.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Mock botbuilder before importing the adapter
mock_bb_core = MagicMock()
mock_bb_schema = MagicMock()
mock_bb_aiohttp = MagicMock()
sys.modules.setdefault("botbuilder", MagicMock())
sys.modules.setdefault("botbuilder.core", mock_bb_core)
sys.modules.setdefault("botbuilder.schema", mock_bb_schema)
sys.modules.setdefault("botbuilder.integration.aiohttp", mock_bb_aiohttp)


from pocketclaw.bus.adapters.teams_adapter import TeamsAdapter  # noqa: E402
from pocketclaw.bus.events import Channel, OutboundMessage  # noqa: E402


class TestTeamsAdapterInit:
    def test_defaults(self):
        adapter = TeamsAdapter()
        assert adapter.app_id == ""
        assert adapter.app_password == ""
        assert adapter.channel == Channel.TEAMS
        assert adapter.webhook_port == 3978

    def test_custom_config(self):
        adapter = TeamsAdapter(
            app_id="app-123",
            app_password="secret",
            allowed_tenant_ids=["tenant-1"],
            webhook_port=4000,
        )
        assert adapter.app_id == "app-123"
        assert adapter.allowed_tenant_ids == ["tenant-1"]
        assert adapter.webhook_port == 4000


class TestTeamsAdapterProcessActivity:
    async def test_process_message(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        # Mock ActivityTypes
        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        activity = SimpleNamespace(
            type="message",
            text="Hello Teams!",
            from_property=SimpleNamespace(id="user-1"),
            conversation=SimpleNamespace(id="conv-1"),
            channel_data=None,
            id="act-1",
            service_url="https://smba.trafficmanager.net",
        )

        turn_ctx = SimpleNamespace(activity=activity)
        await adapter._process_activity(turn_ctx)

        adapter._bus.publish_inbound.assert_called_once()
        call_args = adapter._bus.publish_inbound.call_args[0][0]
        assert call_args.content == "Hello Teams!"
        assert call_args.sender_id == "user-1"
        assert call_args.chat_id == "conv-1"
        assert call_args.channel == Channel.TEAMS

    async def test_skip_non_message_activity(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        activity = SimpleNamespace(
            type="typing",  # not a message
            text="",
        )
        turn_ctx = SimpleNamespace(activity=activity)
        await adapter._process_activity(turn_ctx)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_empty_text_skipped(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        activity = SimpleNamespace(
            type="message",
            text="",
            from_property=SimpleNamespace(id="user"),
            conversation=SimpleNamespace(id="conv"),
            channel_data=None,
            id="act",
            service_url="",
        )
        turn_ctx = SimpleNamespace(activity=activity)
        await adapter._process_activity(turn_ctx)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_tenant_filter(self):
        adapter = TeamsAdapter(
            app_id="app",
            app_password="pw",
            allowed_tenant_ids=["tenant-ok"],
        )
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        mock_bb_schema.ActivityTypes = SimpleNamespace(message="message")

        activity = SimpleNamespace(
            type="message",
            text="blocked",
            from_property=SimpleNamespace(id="user"),
            conversation=SimpleNamespace(id="conv"),
            channel_data=SimpleNamespace(tenant={"id": "tenant-bad"}),
            id="act",
            service_url="",
        )
        turn_ctx = SimpleNamespace(activity=activity)
        await adapter._process_activity(turn_ctx)
        adapter._bus.publish_inbound.assert_not_called()


class TestTeamsAdapterSend:
    async def test_send_normal_message(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._adapter = MagicMock()  # BotFrameworkAdapter mock

        msg = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="conv-1",
            content="Hello!",
        )
        await adapter.send(msg)
        # Should not raise, just log

    async def test_send_stream_accumulates(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._adapter = MagicMock()

        chunk1 = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="c1",
            content="Hello ",
            is_stream_chunk=True,
        )
        chunk2 = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="c1",
            content="World!",
            is_stream_chunk=True,
        )
        await adapter.send(chunk1)
        await adapter.send(chunk2)
        assert adapter._buffers.get("c1") == "Hello World!"

    async def test_send_stream_end_flushes(self):
        adapter = TeamsAdapter(app_id="app", app_password="pw")
        adapter._adapter = MagicMock()

        adapter._buffers["c1"] = "accumulated text"

        end = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="c1",
            content="",
            is_stream_end=True,
        )
        await adapter.send(end)
        assert "c1" not in adapter._buffers

    async def test_send_empty_skipped(self):
        adapter = TeamsAdapter()
        adapter._adapter = MagicMock()

        msg = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="c1",
            content="   ",
        )
        await adapter.send(msg)
        # Should not raise

    async def test_send_without_adapter(self):
        adapter = TeamsAdapter()
        # _adapter is None
        msg = OutboundMessage(
            channel=Channel.TEAMS,
            chat_id="c1",
            content="test",
        )
        await adapter.send(msg)  # should not raise
