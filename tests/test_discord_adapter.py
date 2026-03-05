# Tests for Discord Channel Adapter
# Created: 2026-02-06

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# --- Mock the discord module before importing the adapter ---

mock_discord = types.ModuleType("discord")
mock_discord.Client = MagicMock
mock_discord.Intents = MagicMock()
mock_discord.Intents.default = MagicMock(return_value=MagicMock())

mock_app_commands = types.ModuleType("discord.app_commands")
mock_app_commands.CommandTree = MagicMock


mock_discord.app_commands = mock_app_commands

sys.modules.setdefault("discord", mock_discord)
sys.modules.setdefault("discord.app_commands", mock_app_commands)

from pocketpaw.bus.adapters.discord_adapter import DISCORD_MSG_LIMIT, DiscordAdapter  # noqa: E402
from pocketpaw.bus.events import Channel, InboundMessage, OutboundMessage  # noqa: E402
from pocketpaw.bus.queue import MessageBus  # noqa: E402


@pytest.fixture
def adapter():
    return DiscordAdapter(
        token="test-token",
        allowed_guild_ids=[111, 222],
        allowed_user_ids=[999],
    )


@pytest.fixture
def bus():
    return MessageBus()


def test_channel_property(adapter):
    assert adapter.channel == Channel.DISCORD


async def test_start_stop(adapter, bus):
    """Start subscribes to bus, stop unsubscribes."""
    # Patch _on_start to avoid actually importing discord and connecting
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()

    await adapter.start(bus)
    assert adapter._running is True
    assert adapter._bus is bus

    await adapter.stop()
    assert adapter._running is False


async def test_send_normal_message(adapter, bus):
    """Normal (non-stream) messages are sent to the channel."""
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)

    mock_channel = AsyncMock()
    mock_client = MagicMock()
    mock_client.get_channel = MagicMock(return_value=mock_channel)
    adapter._client = mock_client

    msg = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="Hello Discord!",
    )
    await adapter.send(msg)

    mock_client.get_channel.assert_called_once_with(12345)
    mock_channel.send.assert_called_once_with("Hello Discord!")


async def test_stream_buffering(adapter):
    """Stream chunks are buffered and not sent immediately."""
    mock_channel = AsyncMock()
    mock_sent_msg = MagicMock()
    mock_sent_msg.message_id = 42
    mock_channel.send = AsyncMock(return_value=mock_sent_msg)

    mock_client = MagicMock()
    mock_client.get_channel = MagicMock(return_value=mock_channel)
    adapter._client = mock_client

    chunk1 = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="Hello ",
        is_stream_chunk=True,
    )
    await adapter.send(chunk1)

    assert "12345" in adapter._buffers
    assert adapter._buffers["12345"]["text"] == "Hello "
    # Initial "..." message was sent
    mock_channel.send.assert_called_once_with("...")


async def test_stream_flush(adapter):
    """Stream end flushes the buffer."""
    mock_sent_msg = AsyncMock()
    mock_sent_msg.edit = AsyncMock()

    mock_channel = AsyncMock()
    mock_channel.send = AsyncMock(return_value=mock_sent_msg)

    mock_client = MagicMock()
    mock_client.get_channel = MagicMock(return_value=mock_channel)
    adapter._client = mock_client

    # Manually prime the buffer
    adapter._buffers["12345"] = {
        "discord_message": mock_sent_msg,
        "text": "Complete response",
        "last_update": 0,
    }

    end_msg = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="12345",
        content="",
        is_stream_end=True,
    )
    await adapter.send(end_msg)

    # Buffer should be flushed
    assert "12345" not in adapter._buffers
    mock_sent_msg.edit.assert_called_once_with(content="Complete response")


def test_guild_auth_filtering(adapter):
    """Guild/user auth checks work correctly."""
    # Authorized guild + user
    guild = MagicMock()
    guild.id = 111
    user = MagicMock()
    user.id = 999
    assert adapter._check_auth(guild, user) is True

    # Unauthorized guild
    guild.id = 333
    assert adapter._check_auth(guild, user) is False

    # Unauthorized user
    guild.id = 111
    user.id = 888
    assert adapter._check_auth(guild, user) is False


def test_guild_auth_no_restrictions():
    """No restrictions means all allowed."""
    adapter = DiscordAdapter(token="t")
    guild = MagicMock()
    guild.id = 999
    user = MagicMock()
    user.id = 1
    assert adapter._check_auth(guild, user) is True


def test_guild_auth_dm_no_guild(adapter):
    """DMs (no guild) pass guild check."""
    user = MagicMock()
    user.id = 999
    assert adapter._check_auth(None, user) is True


async def test_inbound_message_creation(adapter, bus):
    """Verify InboundMessage is published to bus correctly."""
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)

    msg = InboundMessage(
        channel=Channel.DISCORD,
        sender_id="999",
        chat_id="12345",
        content="test message",
        metadata={"username": "user#1234"},
    )
    await adapter._publish_inbound(msg)

    assert bus.inbound_pending() == 1
    consumed = await bus.consume_inbound()
    assert consumed.content == "test message"
    assert consumed.channel == Channel.DISCORD


def test_split_message():
    """Messages over 2000 chars are split."""
    short = "Hello"
    assert DiscordAdapter._split_message(short) == ["Hello"]

    long_text = "x" * 3000
    chunks = DiscordAdapter._split_message(long_text)
    assert len(chunks) == 2
    assert len(chunks[0]) == DISCORD_MSG_LIMIT
    assert len(chunks[1]) == 1000

    assert DiscordAdapter._split_message("") == []


async def test_bus_integration(bus):
    """Adapter receives outbound messages from bus subscription."""
    adapter = DiscordAdapter(token="t")
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    adapter.send = AsyncMock()

    await adapter.start(bus)

    msg = OutboundMessage(
        channel=Channel.DISCORD,
        chat_id="123",
        content="response",
    )
    await bus.publish_outbound(msg)

    adapter.send.assert_called_once_with(msg)

    await adapter.stop()
