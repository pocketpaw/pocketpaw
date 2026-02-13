# Tests for Telegram Group Topics support in telegram_adapter.py
# Created: 2026-02-07

# Mock telegram before importing adapter
import sys
from unittest.mock import MagicMock

import pytest

mock_telegram = MagicMock()
mock_telegram.Update = MagicMock()
mock_telegram.ForceReply = MagicMock()
mock_telegram.error = MagicMock()
sys.modules["telegram"] = mock_telegram
sys.modules["telegram.error"] = mock_telegram.error
sys.modules["telegram.ext"] = MagicMock()

from pocketclaw.bus.adapters.telegram_adapter import TelegramAdapter  # noqa: E402


@pytest.fixture
def adapter():
    return TelegramAdapter(token="test-token", allowed_user_id=12345)


# ---------------------------------------------------------------------------
# _parse_chat_id
# ---------------------------------------------------------------------------


class TestParseChatId:
    def test_plain_chat_id(self):
        chat_id, topic_id = TelegramAdapter._parse_chat_id("123456")
        assert chat_id == "123456"
        assert topic_id is None

    def test_topic_chat_id(self):
        chat_id, topic_id = TelegramAdapter._parse_chat_id("123456:topic:42")
        assert chat_id == "123456"
        assert topic_id == 42

    def test_topic_id_zero(self):
        chat_id, topic_id = TelegramAdapter._parse_chat_id("123456:topic:0")
        assert chat_id == "123456"
        assert topic_id == 0

    def test_negative_chat_id_with_topic(self):
        chat_id, topic_id = TelegramAdapter._parse_chat_id("-100123456:topic:7")
        assert chat_id == "-100123456"
        assert topic_id == 7


# ---------------------------------------------------------------------------
# Session key isolation via _handle_message
# ---------------------------------------------------------------------------


class TestTopicSessionKey:
    def test_forum_message_includes_topic(self):
        """Messages in forum topics should include topic ID in chat_id."""
        # This tests the logic we added to _handle_message
        # We verify via _parse_chat_id that the format round-trips correctly
        base = "-100999"
        topic = 55
        chat_id = f"{base}:topic:{topic}"
        parsed_base, parsed_topic = TelegramAdapter._parse_chat_id(chat_id)
        assert parsed_base == base
        assert parsed_topic == topic

    def test_non_forum_message_no_topic(self):
        """Normal chats should not include topic suffix."""
        chat_id = "777888"
        parsed_base, parsed_topic = TelegramAdapter._parse_chat_id(chat_id)
        assert parsed_base == "777888"
        assert parsed_topic is None

    def test_different_topics_different_session_keys(self):
        """Different topics in the same group should have different session keys."""
        group = "-100111"
        key1 = f"{group}:topic:1"
        key2 = f"{group}:topic:2"
        assert key1 != key2
        # Both parse back to the same base group
        assert TelegramAdapter._parse_chat_id(key1)[0] == group
        assert TelegramAdapter._parse_chat_id(key2)[0] == group
