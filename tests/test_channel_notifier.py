"""Tests for the channel notifier."""

from unittest.mock import AsyncMock, MagicMock, patch

from pocketclaw.bus.events import Channel, OutboundMessage
from pocketclaw.bus.notifier import notify


class TestNotify:
    """Tests for notify()."""

    @patch("pocketclaw.bus.get_message_bus")
    async def test_publishes_for_each_target(self, mock_bus_fn):
        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        mock_bus_fn.return_value = bus

        count = await notify("Hello!", targets=["telegram:123", "discord:456"])
        assert count == 2
        assert bus.publish_outbound.call_count == 2

        # First call
        msg1 = bus.publish_outbound.call_args_list[0][0][0]
        assert isinstance(msg1, OutboundMessage)
        assert msg1.channel == Channel.TELEGRAM
        assert msg1.chat_id == "123"
        assert msg1.content == "Hello!"

        # Second call
        msg2 = bus.publish_outbound.call_args_list[1][0][0]
        assert msg2.channel == Channel.DISCORD
        assert msg2.chat_id == "456"

    @patch("pocketclaw.bus.get_message_bus")
    async def test_skips_invalid_target_no_colon(self, mock_bus_fn):
        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        mock_bus_fn.return_value = bus

        count = await notify("Test", targets=["invalid_no_colon"])
        assert count == 0
        bus.publish_outbound.assert_not_called()

    @patch("pocketclaw.bus.get_message_bus")
    async def test_skips_unknown_channel(self, mock_bus_fn):
        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        mock_bus_fn.return_value = bus

        count = await notify("Test", targets=["unknown_channel:123"])
        assert count == 0
        bus.publish_outbound.assert_not_called()

    async def test_returns_zero_empty_targets(self):
        count = await notify("Test", targets=[])
        assert count == 0

    @patch("pocketclaw.bus.get_message_bus")
    @patch("pocketclaw.config.get_settings")
    async def test_reads_from_settings_when_targets_none(self, mock_settings, mock_bus_fn):
        mock_settings.return_value = MagicMock(notification_channels=["slack:C123"])
        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        mock_bus_fn.return_value = bus

        count = await notify("Auto")
        assert count == 1
        msg = bus.publish_outbound.call_args[0][0]
        assert msg.channel == Channel.SLACK
        assert msg.chat_id == "C123"

    @patch("pocketclaw.config.get_settings")
    async def test_returns_zero_no_configured_channels(self, mock_settings):
        mock_settings.return_value = MagicMock(notification_channels=[])
        count = await notify("Nothing")
        assert count == 0

    @patch("pocketclaw.bus.get_message_bus")
    async def test_mixed_valid_invalid_targets(self, mock_bus_fn):
        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        mock_bus_fn.return_value = bus

        count = await notify(
            "Mix",
            targets=["telegram:111", "bad", "whatsapp:222", "nope:333"],
        )
        assert count == 2  # telegram + whatsapp succeed

    @patch("pocketclaw.bus.get_message_bus")
    async def test_chat_id_with_colons(self, mock_bus_fn):
        """Targets like 'matrix:!room:server.org' should split on first colon only."""
        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        mock_bus_fn.return_value = bus

        count = await notify("Matrix", targets=["matrix:!room:server.org"])
        assert count == 1
        msg = bus.publish_outbound.call_args[0][0]
        assert msg.channel == Channel.MATRIX
        assert msg.chat_id == "!room:server.org"

    @patch("pocketclaw.bus.get_message_bus")
    async def test_all_known_channels(self, mock_bus_fn):
        """Every Channel enum value should be recognized."""
        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        mock_bus_fn.return_value = bus

        targets = [f"{c.value}:test_id" for c in Channel]
        count = await notify("Broadcast", targets=targets)
        assert count == len(Channel)

    @patch("pocketclaw.bus.get_message_bus")
    async def test_content_passed_through(self, mock_bus_fn):
        bus = MagicMock()
        bus.publish_outbound = AsyncMock()
        mock_bus_fn.return_value = bus

        msg = "Reminder: Call dentist at 3pm"
        await notify(msg, targets=["telegram:42"])
        sent = bus.publish_outbound.call_args[0][0]
        assert sent.content == msg
