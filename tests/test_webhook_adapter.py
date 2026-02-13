"""Tests for the generic inbound webhook adapter.

Created: 2026-02-09
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pocketclaw.bus.adapters.webhook_adapter import WebhookAdapter, WebhookSlotConfig
from pocketclaw.bus.events import Channel, OutboundMessage


@pytest.fixture
def adapter():
    a = WebhookAdapter()
    a._bus = MagicMock()
    a._bus.publish_inbound = AsyncMock()
    a._running = True
    return a


@pytest.fixture
def slot():
    return WebhookSlotConfig(
        name="test-hook",
        secret="secret123",
        description="Test webhook",
        sync_timeout=5,
    )


class TestWebhookSlotConfig:
    def test_defaults(self):
        cfg = WebhookSlotConfig(name="x", secret="s")
        assert cfg.description == ""
        assert cfg.sync_timeout == 30

    def test_custom_values(self):
        cfg = WebhookSlotConfig(name="gh", secret="sec", description="GitHub", sync_timeout=10)
        assert cfg.name == "gh"
        assert cfg.secret == "sec"
        assert cfg.description == "GitHub"
        assert cfg.sync_timeout == 10


class TestWebhookAdapterProperties:
    def test_channel_is_webhook(self):
        adapter = WebhookAdapter()
        assert adapter.channel == Channel.WEBHOOK

    def test_initial_state(self):
        adapter = WebhookAdapter()
        assert adapter._pending == {}
        assert adapter._buffers == {}


class TestHandleWebhookAsync:
    async def test_standard_payload(self, adapter, slot):
        body = {"content": "hello world", "sender": "user@github"}
        result = await adapter.handle_webhook(slot, body, "req-1", sync=False)

        assert result is None
        adapter._bus.publish_inbound.assert_called_once()
        msg = adapter._bus.publish_inbound.call_args[0][0]
        assert msg.channel == Channel.WEBHOOK
        assert msg.content == "hello world"
        assert msg.sender_id == "user@github"
        assert msg.chat_id == "req-1"
        assert msg.metadata["webhook_name"] == "test-hook"
        assert msg.metadata["source"] == "webhook"

    async def test_raw_fallback(self, adapter, slot):
        """When body has no 'content' key, entire body becomes content."""
        body = {"event": "push", "repo": "myrepo"}
        await adapter.handle_webhook(slot, body, "req-2", sync=False)

        msg = adapter._bus.publish_inbound.call_args[0][0]
        assert msg.content == json.dumps(body)

    async def test_default_sender(self, adapter, slot):
        """When body has no 'sender', default to webhook:<name>."""
        body = {"content": "test"}
        await adapter.handle_webhook(slot, body, "req-3", sync=False)

        msg = adapter._bus.publish_inbound.call_args[0][0]
        assert msg.sender_id == "webhook:test-hook"

    async def test_metadata_merged(self, adapter, slot):
        body = {"content": "test", "metadata": {"repo": "myrepo"}}
        await adapter.handle_webhook(slot, body, "req-4", sync=False)

        msg = adapter._bus.publish_inbound.call_args[0][0]
        assert msg.metadata["repo"] == "myrepo"
        assert msg.metadata["webhook_name"] == "test-hook"

    async def test_non_dict_metadata_ignored(self, adapter, slot):
        body = {"content": "test", "metadata": "not-a-dict"}
        await adapter.handle_webhook(slot, body, "req-5", sync=False)

        msg = adapter._bus.publish_inbound.call_args[0][0]
        assert msg.metadata["webhook_name"] == "test-hook"


class TestHandleWebhookSync:
    async def test_sync_resolves_with_response(self, adapter, slot):
        """Sync mode resolves when send() delivers a non-streaming message."""

        async def respond():
            await asyncio.sleep(0.05)
            out = OutboundMessage(
                channel=Channel.WEBHOOK,
                chat_id="req-sync-1",
                content="Agent says hello",
            )
            await adapter.send(out)

        asyncio.create_task(respond())

        result = await adapter.handle_webhook(slot, {"content": "hi"}, "req-sync-1", sync=True)
        assert result == "Agent says hello"

    async def test_sync_timeout(self, adapter, slot):
        """Sync mode returns None on timeout."""
        slot.sync_timeout = 0.1  # 100ms
        result = await adapter.handle_webhook(slot, {"content": "hi"}, "req-timeout", sync=True)
        assert result is None
        # Pending should be cleaned up
        assert "req-timeout" not in adapter._pending

    async def test_sync_stream_accumulation(self, adapter, slot):
        """Sync mode accumulates stream chunks and resolves on stream_end."""

        async def stream_respond():
            await asyncio.sleep(0.05)
            # Send chunks
            for chunk in ["Hello ", "world", "!"]:
                out = OutboundMessage(
                    channel=Channel.WEBHOOK,
                    chat_id="req-stream",
                    content=chunk,
                    is_stream_chunk=True,
                    is_stream_end=False,
                )
                await adapter.send(out)

            # Send stream end
            end = OutboundMessage(
                channel=Channel.WEBHOOK,
                chat_id="req-stream",
                content="",
                is_stream_chunk=True,
                is_stream_end=True,
            )
            await adapter.send(end)

        asyncio.create_task(stream_respond())

        result = await adapter.handle_webhook(slot, {"content": "hi"}, "req-stream", sync=True)
        assert result == "Hello world!"


class TestSendMethod:
    async def test_send_no_waiter(self, adapter):
        """Send with no pending future just logs (no error)."""
        out = OutboundMessage(channel=Channel.WEBHOOK, chat_id="nobody", content="lost")
        await adapter.send(out)  # Should not raise

    async def test_send_resolves_future(self, adapter):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        adapter._pending["req-x"] = fut

        out = OutboundMessage(channel=Channel.WEBHOOK, chat_id="req-x", content="response")
        await adapter.send(out)

        assert fut.done()
        assert fut.result() == "response"
        assert "req-x" not in adapter._pending

    async def test_send_stream_chunks_accumulate(self, adapter):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        adapter._pending["req-s"] = fut

        for text in ["A", "B"]:
            out = OutboundMessage(
                channel=Channel.WEBHOOK,
                chat_id="req-s",
                content=text,
                is_stream_chunk=True,
            )
            await adapter.send(out)

        # Future should not yet be resolved
        assert not fut.done()
        assert adapter._buffers["req-s"] == ["A", "B"]

        # End stream
        end = OutboundMessage(
            channel=Channel.WEBHOOK,
            chat_id="req-s",
            content="",
            is_stream_chunk=True,
            is_stream_end=True,
        )
        await adapter.send(end)

        assert fut.done()
        assert fut.result() == "AB"
        assert "req-s" not in adapter._buffers
