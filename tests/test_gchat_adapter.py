"""Tests for Google Chat Channel Adapter — Sprint 23.

google-api-python-client is mocked since it's an optional dependency.
"""

import sys
from unittest.mock import AsyncMock, MagicMock

# Mock google libs before importing the adapter
mock_oauth2 = MagicMock()
mock_service_account = MagicMock()
mock_oauth2.service_account = mock_service_account
mock_discovery = MagicMock()
sys.modules.setdefault("google", MagicMock())
sys.modules.setdefault("google.oauth2", mock_oauth2)
sys.modules.setdefault("google.oauth2.service_account", mock_service_account)
sys.modules.setdefault("googleapiclient", MagicMock())
sys.modules.setdefault("googleapiclient.discovery", mock_discovery)


from pocketclaw.bus.adapters.gchat_adapter import GoogleChatAdapter  # noqa: E402
from pocketclaw.bus.events import Channel, OutboundMessage  # noqa: E402


class TestGoogleChatAdapterInit:
    def test_defaults(self):
        adapter = GoogleChatAdapter()
        assert adapter.mode == "webhook"
        assert adapter.service_account_key is None
        assert adapter.channel == Channel.GOOGLE_CHAT
        assert adapter.allowed_space_ids == []

    def test_custom_config(self):
        adapter = GoogleChatAdapter(
            mode="pubsub",
            service_account_key="/path/to/key.json",
            project_id="my-project",
            subscription_id="my-sub",
            allowed_space_ids=["spaces/ABC"],
        )
        assert adapter.mode == "pubsub"
        assert adapter.service_account_key == "/path/to/key.json"
        assert adapter.project_id == "my-project"
        assert adapter.subscription_id == "my-sub"
        assert adapter.allowed_space_ids == ["spaces/ABC"]


class TestGoogleChatAdapterWebhook:
    async def test_handle_valid_message(self):
        adapter = GoogleChatAdapter()
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        payload = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/AAA/messages/111",
                "sender": {"name": "users/123", "displayName": "Alice"},
                "text": "Hello Google Chat!",
                "thread": {"name": "spaces/AAA/threads/t1"},
            },
            "space": {"name": "spaces/AAA"},
        }

        await adapter.handle_webhook_message(payload)

        adapter._bus.publish_inbound.assert_called_once()
        call_args = adapter._bus.publish_inbound.call_args[0][0]
        assert call_args.content == "Hello Google Chat!"
        assert call_args.sender_id == "users/123"
        assert call_args.chat_id == "spaces/AAA"
        assert call_args.channel == Channel.GOOGLE_CHAT
        assert call_args.metadata["sender_display_name"] == "Alice"

    async def test_skip_non_message_event(self):
        adapter = GoogleChatAdapter()
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        payload = {
            "type": "ADDED_TO_SPACE",
            "space": {"name": "spaces/AAA"},
        }

        await adapter.handle_webhook_message(payload)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_empty_text_skipped(self):
        adapter = GoogleChatAdapter()
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        payload = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/AAA/messages/111",
                "sender": {"name": "users/123", "displayName": "Alice"},
                "text": "",
                "thread": {"name": "spaces/AAA/threads/t1"},
            },
            "space": {"name": "spaces/AAA"},
        }

        await adapter.handle_webhook_message(payload)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_space_filter(self):
        adapter = GoogleChatAdapter(allowed_space_ids=["spaces/ALLOWED"])
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        payload = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/OTHER/messages/111",
                "sender": {"name": "users/123", "displayName": "Alice"},
                "text": "blocked",
                "thread": {},
            },
            "space": {"name": "spaces/OTHER"},
        }

        await adapter.handle_webhook_message(payload)
        adapter._bus.publish_inbound.assert_not_called()

    async def test_argument_text_fallback(self):
        """When text is empty but argumentText is set (slash commands)."""
        adapter = GoogleChatAdapter()
        adapter._bus = MagicMock()
        adapter._bus.publish_inbound = AsyncMock()

        payload = {
            "type": "MESSAGE",
            "message": {
                "name": "spaces/AAA/messages/111",
                "sender": {"name": "users/123", "displayName": "Bob"},
                "text": "",
                "argumentText": "/help me",
                "thread": {},
            },
            "space": {"name": "spaces/AAA"},
        }

        await adapter.handle_webhook_message(payload)
        adapter._bus.publish_inbound.assert_called_once()
        call_args = adapter._bus.publish_inbound.call_args[0][0]
        assert call_args.content == "/help me"


class TestGoogleChatAdapterSend:
    async def test_send_normal_message(self):
        adapter = GoogleChatAdapter()
        mock_service = MagicMock()
        mock_service.spaces().messages().create().execute = MagicMock()
        adapter._chat_service = mock_service

        msg = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/AAA",
            content="Hello!",
        )
        await adapter.send(msg)

    async def test_send_stream_accumulates(self):
        adapter = GoogleChatAdapter()
        mock_service = MagicMock()
        adapter._chat_service = mock_service

        chunk1 = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="Hello ",
            is_stream_chunk=True,
        )
        chunk2 = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="World!",
            is_stream_chunk=True,
        )
        await adapter.send(chunk1)
        await adapter.send(chunk2)
        assert adapter._buffers.get("spaces/A") == "Hello World!"

    async def test_send_stream_end_flushes(self):
        adapter = GoogleChatAdapter()
        mock_service = MagicMock()
        adapter._chat_service = mock_service

        adapter._buffers["spaces/A"] = "accumulated text"

        end = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="",
            is_stream_end=True,
        )
        await adapter.send(end)
        assert "spaces/A" not in adapter._buffers

    async def test_send_empty_skipped(self):
        adapter = GoogleChatAdapter()
        adapter._chat_service = MagicMock()

        msg = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="   ",
        )
        await adapter.send(msg)

    async def test_send_without_service(self):
        adapter = GoogleChatAdapter()
        # _chat_service is None
        msg = OutboundMessage(
            channel=Channel.GOOGLE_CHAT,
            chat_id="spaces/A",
            content="test",
        )
        await adapter.send(msg)  # should not raise
