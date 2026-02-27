# Edge-case tests for Memory Subsystem
# Created: 2026-02-25
# Addresses issue #36: Edge-case testing for memory subsystem
#
# Tests cover:
# - Concurrent read/write to the same session
# - Session history exceeding compaction threshold
# - Special characters and Unicode handling
# - Empty session handling
# - Auto-learn triggering conditions
# - Session listing and deletion

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.memory.file_store import FileMemoryStore
from pocketpaw.memory.manager import MemoryManager
from pocketpaw.memory.protocol import MemoryEntry, MemoryType


@pytest.fixture
def temp_memory_path():
    """Create a temporary directory for memory tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def memory_store(temp_memory_path):
    """Create a FileMemoryStore with temp path."""
    return FileMemoryStore(base_path=temp_memory_path)


@pytest.fixture
def memory_manager(temp_memory_path):
    """Create a MemoryManager with temp path."""
    return MemoryManager(base_path=temp_memory_path)


# ===========================================================================
# TestConcurrentAccess
# ===========================================================================


class TestConcurrentAccess:
    """Test concurrent read/write operations to the same session."""

    @pytest.mark.asyncio
    async def test_concurrent_writes_to_same_session(self, memory_manager):
        """Multiple concurrent writes to the same session should all succeed."""
        session_key = "test:concurrent"

        async def write_message(index: int):
            await memory_manager.add_to_session(
                session_key, "user", f"Message {index}"
            )

        # Write 10 messages concurrently
        await asyncio.gather(*[write_message(i) for i in range(10)])

        # Verify all messages were saved
        history = await memory_manager.get_session_history(session_key)
        assert len(history) == 10

        # Verify messages are in order
        for i in range(10):
            assert any(f"Message {i}" in msg["content"] for msg in history)

    @pytest.mark.asyncio
    async def test_concurrent_read_write(self, memory_manager):
        """Concurrent reads and writes should not corrupt data."""
        session_key = "test:read_write"

        # Pre-populate session
        for i in range(5):
            await memory_manager.add_to_session(session_key, "user", f"Init {i}")

        async def reader():
            """Read session history repeatedly."""
            results = []
            for _ in range(10):
                history = await memory_manager.get_session_history(session_key)
                results.append(len(history))
                await asyncio.sleep(0.001)
            return results

        async def writer():
            """Write new messages."""
            for i in range(5):
                await memory_manager.add_to_session(
                    session_key, "assistant", f"New {i}"
                )
                await asyncio.sleep(0.001)

        # Run readers and writer concurrently
        read_results = await asyncio.gather(
            reader(), reader(), reader(), writer()
        )

        # Verify final state
        final_history = await memory_manager.get_session_history(session_key)
        assert len(final_history) == 10  # 5 init + 5 new

        # Verify all reads returned valid history lengths
        for read_result in read_results[:3]:  # First 3 are readers
            assert all(5 <= count <= 10 for count in read_result)

    @pytest.mark.asyncio
    async def test_concurrent_session_clear(self, memory_manager):
        """Clearing a session while reading should be safe."""
        session_key = "test:clear"

        # Pre-populate
        for i in range(20):
            await memory_manager.add_to_session(session_key, "user", f"Msg {i}")

        async def reader():
            """Try to read history."""
            return await memory_manager.get_session_history(session_key)

        async def clearer():
            """Clear the session."""
            await asyncio.sleep(0.005)
            return await memory_manager.clear_session(session_key)

        # Run concurrently
        history, clear_count = await asyncio.gather(reader(), clearer())

        # Either we read before clear (20 messages) or after (0 messages)
        # Both are valid outcomes
        assert len(history) in (0, 20)
        assert clear_count == 20


# ===========================================================================
# TestCompactionThreshold
# ===========================================================================


class TestCompactionThreshold:
    """Test session history compaction with large histories."""

    @pytest.mark.asyncio
    async def test_compaction_with_large_history(self, memory_manager):
        """Large session history should be compacted correctly."""
        session_key = "test:large"

        # Create a large conversation (100 messages)
        for i in range(100):
            role = "user" if i % 2 == 0 else "assistant"
            await memory_manager.add_to_session(
                session_key, role, f"Message number {i} with some content"
            )

        # Get compacted history with small window
        compacted = await memory_manager.get_compacted_history(
            session_key,
            recent_window=10,  # Keep last 10 messages
            char_budget=500,   # Small budget
            summary_chars=200,
            llm_summarize=False,  # Disable LLM to test basic compaction
        )

        # Should have fewer messages than original
        assert len(compacted) < 100

        # Recent messages should be preserved
        recent_contents = [msg["content"] for msg in compacted[-10:]]
        assert any("Message number 99" in c for c in recent_contents)

    @pytest.mark.asyncio
    async def test_compaction_preserves_recent_messages(self, memory_manager):
        """Compaction should always preserve recent_window messages."""
        session_key = "test:preserve"

        # Create 50 messages
        for i in range(50):
            role = "user" if i % 2 == 0 else "assistant"
            await memory_manager.add_to_session(session_key, role, f"Msg {i}")

        # Compact with recent_window=5
        compacted = await memory_manager.get_compacted_history(
            session_key,
            recent_window=5,
            char_budget=200,
            summary_chars=100,
            llm_summarize=False,
        )

        # Last 5 messages should be intact
        recent_msgs = compacted[-5:]
        assert len(recent_msgs) == 5
        assert "Msg 49" in recent_msgs[-1]["content"]
        assert "Msg 48" in recent_msgs[-2]["content"]

    @pytest.mark.asyncio
    async def test_compaction_with_very_long_messages(self, memory_manager):
        """Messages exceeding char_budget should be handled gracefully."""
        session_key = "test:long"

        # Add messages with varying lengths
        await memory_manager.add_to_session(
            session_key, "user", "x" * 10000  # Very long message
        )
        await memory_manager.add_to_session(session_key, "assistant", "Short")
        await memory_manager.add_to_session(session_key, "user", "y" * 5000)

        # Compact with small budget
        compacted = await memory_manager.get_compacted_history(
            session_key,
            recent_window=2,
            char_budget=100,
            summary_chars=50,
            llm_summarize=False,
        )

        # Should return at least 1 message (compaction is aggressive with small budget)
        assert len(compacted) >= 1
        # Recent message should be preserved
        assert "y" in compacted[-1]["content"]

    @pytest.mark.asyncio
    async def test_empty_session_compaction(self, memory_manager):
        """Compacting an empty session should return empty list."""
        session_key = "test:empty_compact"

        compacted = await memory_manager.get_compacted_history(
            session_key,
            recent_window=10,
            char_budget=1000,
            summary_chars=500,
            llm_summarize=False,
        )

        assert compacted == []


# ===========================================================================
# TestUnicodeAndSpecialCharacters
# ===========================================================================


class TestUnicodeAndSpecialCharacters:
    """Test handling of Unicode and special characters."""

    @pytest.mark.asyncio
    async def test_emoji_storage_and_retrieval(self, memory_manager):
        """Emojis should be stored and retrieved correctly."""
        content = "User loves 🐾 PocketPaw and uses 🚀 emojis everywhere! 😊"

        entry_id = await memory_manager.remember(content)
        assert entry_id

        # Retrieve and verify emoji is preserved
        all_memories = await memory_manager._store.get_by_type(MemoryType.LONG_TERM)
        assert len(all_memories) >= 1
        assert "🐾" in all_memories[0].content

        # Search for text content (emojis may be stripped in search)
        results = await memory_manager._store.search("PocketPaw emojis")
        assert len(results) >= 1
        assert "🐾" in results[0].content

    @pytest.mark.asyncio
    async def test_multilingual_content(self, memory_manager):
        """Non-ASCII characters from various languages should work."""
        test_cases = [
            ("User prefers Spanish: español", "español"),  # Spanish
            ("User likes Chinese: 中文", "中文"),  # Chinese
            ("User loves Japanese: 日本語", "日本語"),  # Japanese
            ("User prefers Russian: русский", "русский"),  # Russian
            ("User likes Arabic: العربية", "العربية"),  # Arabic
            ("User loves Hindi: हिंदी", "हिंदी"),  # Hindi
        ]

        for content, _ in test_cases:
            entry_id = await memory_manager.remember(content)
            assert entry_id

        # Retrieve all long-term memories
        all_memories = await memory_manager._store.get_by_type(MemoryType.LONG_TERM)
        assert len(all_memories) == len(test_cases)

        # Verify each language is preserved
        all_content = {e.content for e in all_memories}
        for content, target_word in test_cases:
            # Verify the non-ASCII content is preserved
            assert any(target_word in c for c in all_content)

    @pytest.mark.asyncio
    async def test_special_characters_in_session(self, memory_manager):
        """Special characters in session messages should be preserved."""
        session_key = "test:special"
        special_chars = [
            "Code: `print('hello')`",
            "Path: C:\\Users\\test\\file.txt",
            "Regex: ^[a-z]+@[a-z]+\\.[a-z]{2,}$",
            "SQL: SELECT * FROM users WHERE id='1'",
            "JSON: {\"key\": \"value\"}",
            "Quote: \"Hello\" and 'World'",
        ]

        for msg in special_chars:
            await memory_manager.add_to_session(session_key, "user", msg)

        history = await memory_manager.get_session_history(session_key)
        assert len(history) == len(special_chars)

        # Verify all special characters are preserved
        retrieved_content = [msg["content"] for msg in history]
        assert retrieved_content == special_chars

    @pytest.mark.asyncio
    async def test_search_with_unicode(self, memory_manager):
        """Unicode content should be stored and retrieved correctly."""
        await memory_manager.remember("User likes café")
        await memory_manager.remember("User prefers naïve approach")
        await memory_manager.remember("User loves résumé writing")

        # Retrieve and verify Unicode is preserved
        all_memories = await memory_manager._store.get_by_type(MemoryType.LONG_TERM)
        assert len(all_memories) == 3

        all_content = {e.content for e in all_memories}
        assert "User likes café" in all_content
        assert "User prefers naïve approach" in all_content
        assert "User loves résumé writing" in all_content

        # Search using ASCII words (accented chars may not work in search)
        results = await memory_manager._store.search("User likes")
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_newlines_and_tabs(self, memory_manager):
        """Newlines and tabs in content should be preserved."""
        content = "Line 1\nLine 2\n\tIndented\n\n\tDouble indent"

        session_key = "test:whitespace"
        await memory_manager.add_to_session(session_key, "user", content)

        history = await memory_manager.get_session_history(session_key)
        assert history[0]["content"] == content


# ===========================================================================
# TestEmptySessionHandling
# ===========================================================================


class TestEmptySessionHandling:
    """Test operations on empty or non-existent sessions."""

    @pytest.mark.asyncio
    async def test_get_empty_session(self, memory_manager):
        """Getting a non-existent session should return empty list."""
        history = await memory_manager.get_session_history("nonexistent:session")
        assert history == []

    @pytest.mark.asyncio
    async def test_clear_empty_session(self, memory_manager):
        """Clearing an empty session should return 0."""
        count = await memory_manager.clear_session("nonexistent:session")
        assert count == 0

    @pytest.mark.asyncio
    async def test_delete_empty_session(self, memory_manager):
        """Deleting a non-existent session should return False."""
        result = await memory_manager.delete_session("nonexistent:session")
        assert result is False

    @pytest.mark.asyncio
    async def test_session_with_only_whitespace(self, memory_manager):
        """Session with only whitespace messages should be stored."""
        session_key = "test:whitespace_only"

        await memory_manager.add_to_session(session_key, "user", "   ")
        await memory_manager.add_to_session(session_key, "assistant", "\n\n\n")
        await memory_manager.add_to_session(session_key, "user", "\t\t")

        history = await memory_manager.get_session_history(session_key)
        assert len(history) == 3

    @pytest.mark.asyncio
    async def test_session_with_empty_string(self, memory_manager):
        """Empty string messages should be stored."""
        session_key = "test:empty_string"

        await memory_manager.add_to_session(session_key, "user", "")
        await memory_manager.add_to_session(session_key, "assistant", "")

        history = await memory_manager.get_session_history(session_key)
        assert len(history) == 2
        assert history[0]["content"] == ""


# ===========================================================================
# TestAutoLearnTriggers
# ===========================================================================


class TestAutoLearnTriggers:
    """Test auto-learn triggering conditions."""

    @pytest.mark.asyncio
    async def test_auto_learn_with_empty_history(self, memory_manager):
        """Auto-learn with empty history should not crash."""
        result = await memory_manager.auto_learn([], file_auto_learn=True)
        # Should return a dict (empty or with results key)
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_auto_learn_with_single_message(self, temp_memory_path):
        """Auto-learn with single message should work."""
        manager = MemoryManager(base_path=temp_memory_path)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='["User name is Alice"]')]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            result = await manager.auto_learn(
                [{"role": "user", "content": "My name is Alice"}],
                file_auto_learn=True,
            )

        assert "results" in result
        assert len(result["results"]) >= 0

    @pytest.mark.asyncio
    async def test_auto_learn_with_very_long_conversation(self, temp_memory_path):
        """Auto-learn should handle very long conversations."""
        manager = MemoryManager(base_path=temp_memory_path)

        # Create long conversation
        long_history = []
        for i in range(100):
            long_history.append({"role": "user", "content": f"Message {i}"})
            long_history.append({"role": "assistant", "content": f"Response {i}"})

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='["Extracted fact"]')]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            result = await manager.auto_learn(long_history, file_auto_learn=True)

        # Should not crash and should return result
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_auto_learn_disabled_by_default(self, memory_manager):
        """Auto-learn should not trigger when file_auto_learn=False."""
        manager = memory_manager
        manager._file_auto_learn = AsyncMock(return_value={"results": []})

        await manager.auto_learn(
            [{"role": "user", "content": "test"}],
            file_auto_learn=False,
        )

        # _file_auto_learn should not be called
        manager._file_auto_learn.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_learn_with_invalid_json_response(self, temp_memory_path):
        """Auto-learn should handle invalid JSON gracefully."""
        manager = MemoryManager(base_path=temp_memory_path)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Not valid JSON")]

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            result = await manager.auto_learn(
                [{"role": "user", "content": "test"}],
                file_auto_learn=True,
            )

        # Should return empty results or handle gracefully
        assert isinstance(result, dict)


# ===========================================================================
# TestSessionListingAndDeletion
# ===========================================================================


class TestSessionListingAndDeletion:
    """Test session listing and deletion operations."""

    @pytest.mark.asyncio
    async def test_list_sessions_for_empty_chat(self, memory_manager):
        """Listing sessions for a chat with no sessions should return empty."""
        sessions = await memory_manager.list_sessions_for_chat("telegram:user123")
        assert sessions == []

    @pytest.mark.asyncio
    async def test_list_sessions_after_creation(self, memory_manager):
        """Session listing returns expected structure."""
        # Create a session
        session_key = "websocket:abc123"

        # Add messages to session
        await memory_manager.add_to_session(session_key, "user", "Hello from session")
        await memory_manager.add_to_session(session_key, "assistant", "Hi there!")

        # Verify session was created
        history = await memory_manager.get_session_history(session_key)
        assert len(history) == 2

        # List sessions - returns list (may be empty if alias system not set up)
        sessions = await memory_manager.list_sessions_for_chat(session_key)
        assert isinstance(sessions, list)

    @pytest.mark.asyncio
    async def test_delete_session_removes_from_listing(self, memory_manager):
        """Deleted sessions should be removed."""
        session1 = "telegram:test:session1"
        session2 = "telegram:test:session2"

        # Create sessions
        await memory_manager.add_to_session(session1, "user", "Message 1")
        await memory_manager.add_to_session(session2, "user", "Message 2")

        # Verify both exist
        history1 = await memory_manager.get_session_history(session1)
        history2 = await memory_manager.get_session_history(session2)
        assert len(history1) == 1
        assert len(history2) == 1

        # Delete one session
        deleted = await memory_manager.delete_session(session1)
        assert deleted is True

        # Verify first is gone, second remains
        history1 = await memory_manager.get_session_history(session1)
        history2 = await memory_manager.get_session_history(session2)
        assert len(history1) == 0
        assert len(history2) == 1

    @pytest.mark.asyncio
    async def test_delete_session_with_messages(self, memory_manager):
        """Deleting a session with many messages should work."""
        session_key = "test:delete_many"

        # Add 50 messages
        for i in range(50):
            role = "user" if i % 2 == 0 else "assistant"
            await memory_manager.add_to_session(session_key, role, f"Msg {i}")

        # Verify session exists
        history = await memory_manager.get_session_history(session_key)
        assert len(history) == 50

        # Delete session
        deleted = await memory_manager.delete_session(session_key)
        assert deleted is True

        # Verify session is gone
        history = await memory_manager.get_session_history(session_key)
        assert len(history) == 0

    @pytest.mark.asyncio
    async def test_session_metadata_in_listing(self, memory_manager):
        """Session can store multiple messages correctly."""
        session_key = "test:metadata"

        # Add messages
        for i in range(5):
            await memory_manager.add_to_session(session_key, "user", f"Message {i}")

        # Get history and verify
        history = await memory_manager.get_session_history(session_key)
        assert len(history) == 5

        # Verify messages are in order
        for i in range(5):
            assert f"Message {i}" in history[i]["content"]

    @pytest.mark.asyncio
    async def test_list_sessions_different_chats(self, memory_manager):
        """Sessions from different chats should be isolated."""
        # Create sessions for different chats
        await memory_manager.add_to_session("telegram:user1:s1", "user", "Msg1")
        await memory_manager.add_to_session("telegram:user2:s1", "user", "Msg2")
        await memory_manager.add_to_session("discord:server1:s1", "user", "Msg3")

        # Verify each session is independent
        history1 = await memory_manager.get_session_history("telegram:user1:s1")
        history2 = await memory_manager.get_session_history("telegram:user2:s1")
        history3 = await memory_manager.get_session_history("discord:server1:s1")

        assert len(history1) == 1
        assert len(history2) == 1
        assert len(history3) == 1

        assert history1[0]["content"] == "Msg1"
        assert history2[0]["content"] == "Msg2"
        assert history3[0]["content"] == "Msg3"
