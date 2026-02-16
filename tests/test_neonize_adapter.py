# Tests for Neonize (WhatsApp Personal) Channel Adapter
# Created: 2026-02-06

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --- Mock the neonize module before importing the adapter ---

mock_neonize = types.ModuleType("neonize")
mock_aioze = types.ModuleType("neonize.aioze")
mock_aioze_client = types.ModuleType("neonize.aioze.client")
mock_aioze_events = types.ModuleType("neonize.aioze.events")
mock_utils = types.ModuleType("neonize.utils")

mock_aioze_client.NewAClient = MagicMock
mock_aioze_events.MessageEv = MagicMock
mock_aioze_events.ConnectedEv = MagicMock
mock_utils.build_jid = MagicMock(side_effect=lambda x: f"jid:{x}")

mock_neonize.aioze = mock_aioze
mock_aioze.client = mock_aioze_client
mock_aioze.events = mock_aioze_events
mock_neonize.utils = mock_utils

sys.modules.setdefault("neonize", mock_neonize)
sys.modules.setdefault("neonize.aioze", mock_aioze)
sys.modules.setdefault("neonize.aioze.client", mock_aioze_client)
sys.modules.setdefault("neonize.aioze.events", mock_aioze_events)
sys.modules.setdefault("neonize.utils", mock_utils)

from pocketpaw.bus.adapters.neonize_adapter import NeonizeAdapter  # noqa: E402
from pocketpaw.bus.events import Channel, OutboundMessage  # noqa: E402
from pocketpaw.bus.queue import MessageBus  # noqa: E402


@pytest.fixture
def adapter():
    return NeonizeAdapter(db_path="/tmp/test_neonize.sqlite3")


@pytest.fixture
def bus():
    return MessageBus()


def test_channel_property(adapter):
    """Channel property returns WHATSAPP."""
    assert adapter.channel == Channel.WHATSAPP


def test_default_db_path():
    """Default db_path uses ~/.pocketpaw/neonize.sqlite3."""
    a = NeonizeAdapter()
    assert "neonize.sqlite3" in a._db_path
    assert ".pocketpaw" in a._db_path


def test_custom_db_path():
    """Custom db_path is respected."""
    a = NeonizeAdapter(db_path="/my/custom/path.db")
    assert a._db_path == "/my/custom/path.db"


async def test_import_error_auto_installs():
    """_on_start tries to auto-install neonize when missing and raises on failure."""
    import builtins
    import subprocess

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name.startswith("neonize"):
            raise ImportError("No module named 'neonize'")
        return real_import(name, *args, **kwargs)

    # Remove cached modules and block re-import to simulate missing neonize
    saved = {}
    for key in list(sys.modules.keys()):
        if key.startswith("neonize"):
            saved[key] = sys.modules.pop(key)

    # Mock subprocess.run so auto_install doesn't actually run pip/uv
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stderr = "package not found"

    try:
        with (
            patch.object(builtins, "__import__", side_effect=_blocked_import),
            patch.object(subprocess, "run", return_value=fake_result),
        ):
            a = NeonizeAdapter()
            with pytest.raises(RuntimeError, match="Failed to install"):
                await a._on_start()
    finally:
        sys.modules.update(saved)


async def test_start_stop(adapter, bus):
    """Start subscribes to bus, stop unsubscribes."""
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()

    await adapter.start(bus)
    assert adapter._running is True
    assert adapter._bus is bus

    await adapter.stop()
    assert adapter._running is False


async def test_qr_data_attribute(adapter):
    """QR data is exposed via _qr_data attribute."""
    assert adapter._qr_data is None
    adapter._qr_data = "test-qr-data-string"
    assert adapter._qr_data == "test-qr-data-string"


async def test_connected_attribute(adapter):
    """Connected state is exposed via _connected attribute."""
    assert adapter._connected is False
    adapter._connected = True
    assert adapter._connected is True


async def test_send_normal_message(adapter, bus):
    """Normal message calls _send_text using cached JID."""
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)

    adapter._connected = True
    adapter._client = MagicMock()
    adapter._client.send_message = AsyncMock()

    # Simulate a cached JID (as would be stored when a message arrives)
    fake_jid = MagicMock()
    adapter._jid_cache["1234567890@s.whatsapp.net"] = fake_jid

    msg = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="1234567890@s.whatsapp.net",
        content="Hello from neonize!",
    )
    await adapter.send(msg)

    adapter._client.send_message.assert_called_once_with(fake_jid, "Hello from neonize!")


async def test_send_skipped_when_not_connected(adapter):
    """Messages are not sent when adapter is not connected."""
    adapter._client = MagicMock()
    adapter._client.send_message = AsyncMock()
    adapter._connected = False

    msg = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="123",
        content="Hello",
    )
    await adapter.send(msg)

    adapter._client.send_message.assert_not_called()


async def test_stream_chunk_buffering(adapter):
    """Stream chunks are buffered correctly."""
    adapter._connected = True
    adapter._client = MagicMock()

    chunk1 = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="123",
        content="Hello ",
        is_stream_chunk=True,
    )
    await adapter.send(chunk1)

    assert "123" in adapter._buffers
    assert adapter._buffers["123"] == "Hello "

    chunk2 = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="123",
        content="World!",
        is_stream_chunk=True,
    )
    await adapter.send(chunk2)

    assert adapter._buffers["123"] == "Hello World!"


async def test_stream_end_flushes_buffer(adapter, bus):
    """Stream end flushes the accumulated buffer."""
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    await adapter.start(bus)

    adapter._connected = True
    adapter._client = MagicMock()
    adapter._client.send_message = AsyncMock()

    # Simulate a cached JID
    fake_jid = MagicMock()
    adapter._jid_cache["123@s.whatsapp.net"] = fake_jid

    # Prime the buffer
    adapter._buffers["123@s.whatsapp.net"] = "Complete response text"

    end_msg = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="123@s.whatsapp.net",
        content="",
        is_stream_end=True,
    )
    await adapter.send(end_msg)

    # Buffer should be flushed
    assert "123@s.whatsapp.net" not in adapter._buffers
    adapter._client.send_message.assert_called_once_with(fake_jid, "Complete response text")


async def test_stream_end_empty_buffer_no_send(adapter):
    """Stream end with empty buffer does not send."""
    adapter._connected = True
    adapter._client = MagicMock()
    adapter._client.send_message = AsyncMock()

    end_msg = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="123",
        content="",
        is_stream_end=True,
    )
    await adapter.send(end_msg)

    adapter._client.send_message.assert_not_called()


async def test_send_empty_message_ignored(adapter):
    """Empty normal messages are not sent."""
    adapter._connected = True
    adapter._client = MagicMock()
    adapter._client.send_message = AsyncMock()

    msg = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="123",
        content="   ",
    )
    await adapter.send(msg)

    adapter._client.send_message.assert_not_called()


async def test_on_stop_disconnects(adapter):
    """_on_stop disconnects the client."""
    mock_client = MagicMock()
    mock_client.disconnect = AsyncMock()
    adapter._client = mock_client
    adapter._connected = True
    adapter._qr_data = "some-qr"

    await adapter._on_stop()

    mock_client.disconnect.assert_called_once()
    assert adapter._connected is False
    assert adapter._qr_data is None


async def test_bus_integration(bus):
    """Adapter receives outbound messages from bus subscription."""
    adapter = NeonizeAdapter()
    adapter._on_start = AsyncMock()
    adapter._on_stop = AsyncMock()
    adapter.send = AsyncMock()

    await adapter.start(bus)

    msg = OutboundMessage(
        channel=Channel.WHATSAPP,
        chat_id="123",
        content="response",
    )
    await bus.publish_outbound(msg)

    adapter.send.assert_called_once_with(msg)

    await adapter.stop()


async def test_preflight_check_passes_when_reachable():
    """Preflight check succeeds when the host is reachable."""
    with patch(
        "pocketpaw.bus.adapters.neonize_adapter._tcp_probe", return_value=None
    ):
        # Should not raise
        await NeonizeAdapter._preflight_connectivity_check()


async def test_preflight_check_raises_when_unreachable():
    """Preflight check raises ConnectionError when host is unreachable."""
    with patch(
        "pocketpaw.bus.adapters.neonize_adapter._tcp_probe",
        side_effect=OSError("Connection refused"),
    ):
        with pytest.raises(ConnectionError, match="Cannot reach"):
            await NeonizeAdapter._preflight_connectivity_check()


async def test_preflight_error_message_mentions_vpn():
    """Error message hints at VPN/network issues."""
    with patch(
        "pocketpaw.bus.adapters.neonize_adapter._tcp_probe",
        side_effect=OSError("timed out"),
    ):
        with pytest.raises(ConnectionError, match="VPN"):
            await NeonizeAdapter._preflight_connectivity_check()
