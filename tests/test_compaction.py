"""Tests for session history compaction (Tier 1 + Tier 2)."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pocketclaw.memory.manager import MemoryManager
from pocketclaw.memory.protocol import MemoryEntry, MemoryType


def _make_entries(n: int, content_len: int = 50) -> list[MemoryEntry]:
    """Create n session entries with predictable content."""
    entries = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        text = f"Message {i}: " + "x" * content_len
        entries.append(
            MemoryEntry(
                id=str(i),
                type=MemoryType.SESSION,
                content=text,
                role=role,
                session_key="test",
            )
        )
    return entries


def _make_manager(entries: list[MemoryEntry], has_sessions_path: bool = True) -> MemoryManager:
    """Create a MemoryManager with a mock store returning the given entries."""
    store = AsyncMock()
    store.get_session = AsyncMock(return_value=entries)
    if has_sessions_path:
        store.sessions_path = Path("/tmp/test_sessions")
    else:
        # Simulate Mem0 backend without sessions_path
        if hasattr(store, "sessions_path"):
            del store.sessions_path
    return MemoryManager(store=store)


# ─── Tier 1: Extract-based compaction ───────────────────────────────────


class TestTier1Compaction:
    async def test_short_session_unchanged(self):
        """Sessions shorter than recent_window are returned as-is."""
        entries = _make_entries(5)
        mgr = _make_manager(entries)
        result = await mgr.get_compacted_history("test", recent_window=10)
        assert len(result) == 5
        assert result[0]["content"] == entries[0].content

    async def test_empty_session(self):
        """Empty session returns empty list."""
        mgr = _make_manager([])
        result = await mgr.get_compacted_history("test")
        assert result == []

    async def test_exact_window_no_summary(self):
        """When message count equals recent_window, no summary block is created."""
        entries = _make_entries(10)
        mgr = _make_manager(entries)
        result = await mgr.get_compacted_history("test", recent_window=10)
        assert len(result) == 10
        # No "[Earlier conversation]" block
        assert not any("[Earlier conversation]" in m["content"] for m in result)

    async def test_older_messages_collapsed(self):
        """Messages beyond recent_window are collapsed into a summary block."""
        entries = _make_entries(15)
        mgr = _make_manager(entries)
        result = await mgr.get_compacted_history("test", recent_window=10, char_budget=50000)
        # First message should be the summary block
        assert result[0]["content"].startswith("[Earlier conversation]")
        assert result[0]["role"] == "user"
        # Recent 10 messages follow
        assert len(result) == 11  # 1 summary + 10 recent

    async def test_summary_contains_older_roles(self):
        """The summary block contains role prefixes from older messages."""
        entries = _make_entries(12)
        mgr = _make_manager(entries)
        result = await mgr.get_compacted_history("test", recent_window=10, char_budget=50000)
        summary = result[0]["content"]
        assert "User:" in summary
        assert "Assistant:" in summary

    async def test_long_messages_truncated_in_summary(self):
        """Older messages longer than summary_chars are truncated."""
        entries = _make_entries(12, content_len=300)
        mgr = _make_manager(entries)
        result = await mgr.get_compacted_history(
            "test", recent_window=10, summary_chars=100, char_budget=50000
        )
        summary = result[0]["content"]
        # Each older message line should end with "..."
        for line in summary.split("\n")[1:]:  # skip "[Earlier conversation]"
            if line.strip():
                assert line.endswith("...")

    async def test_recent_messages_verbatim(self):
        """Recent messages are kept exactly as-is."""
        entries = _make_entries(15, content_len=30)
        mgr = _make_manager(entries)
        result = await mgr.get_compacted_history("test", recent_window=5, char_budget=50000)
        # Last 5 messages should match entries[-5:]
        for i, msg in enumerate(result[-5:]):
            assert msg["content"] == entries[-(5 - i)].content


# ─── Budget enforcement ─────────────────────────────────────────────────


class TestBudgetEnforcement:
    async def test_under_budget_unchanged(self):
        """Messages within budget are returned unchanged."""
        messages = [{"role": "user", "content": "short"}]
        result = MemoryManager._enforce_budget(messages, char_budget=1000)
        assert result == messages

    async def test_over_budget_drops_oldest(self):
        """When over budget, oldest messages are dropped first."""
        messages = [
            {"role": "user", "content": "a" * 500},
            {"role": "assistant", "content": "b" * 500},
            {"role": "user", "content": "c" * 100},
        ]
        result = MemoryManager._enforce_budget(messages, char_budget=700)
        # Should have dropped the first message
        assert len(result) == 2
        assert result[-1]["content"] == "c" * 100

    async def test_single_message_truncated(self):
        """A single message exceeding budget is truncated."""
        messages = [{"role": "user", "content": "x" * 5000}]
        result = MemoryManager._enforce_budget(messages, char_budget=200)
        assert len(result) == 1
        assert len(result[0]["content"]) == 200

    async def test_budget_preserves_newest(self):
        """Budget enforcement always preserves the newest message."""
        messages = [
            {"role": "user", "content": "a" * 300},
            {"role": "assistant", "content": "b" * 300},
            {"role": "user", "content": "newest"},
        ]
        result = MemoryManager._enforce_budget(messages, char_budget=350)
        assert result[-1]["content"] == "newest"

    async def test_compacted_history_respects_budget(self):
        """Full compaction pipeline respects char_budget."""
        entries = _make_entries(20, content_len=200)
        mgr = _make_manager(entries)
        result = await mgr.get_compacted_history("test", recent_window=5, char_budget=2000)
        total = sum(len(m["content"]) for m in result)
        assert total <= 2000


# ─── Tier 2: LLM summary ────────────────────────────────────────────────


class TestTier2LLMSummary:
    async def test_llm_summary_called(self, tmp_path):
        """LLM summary is called when enabled."""
        entries = _make_entries(15)
        store = AsyncMock()
        store.get_session = AsyncMock(return_value=entries)
        store.sessions_path = tmp_path
        mgr = MemoryManager(store=store)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Summary of conversation.")]

        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            result = await mgr.get_compacted_history(
                "test", recent_window=10, llm_summarize=True, char_budget=50000
            )

        assert result[0]["content"].startswith("[Earlier conversation]")
        assert "Summary of conversation." in result[0]["content"]
        mock_client.messages.create.assert_called_once()

    async def test_cached_summary_reused(self, tmp_path):
        """Cached summary is reused when watermark matches."""
        entries = _make_entries(15)
        store = AsyncMock()
        store.get_session = AsyncMock(return_value=entries)
        store.sessions_path = tmp_path

        # Write cache with matching watermark
        cache_file = tmp_path / "test_compaction.json"
        cache_file.write_text(
            json.dumps({"watermark": 15, "summary": "Cached summary.", "older_count": 5})
        )

        mgr = MemoryManager(store=store)
        result = await mgr.get_compacted_history(
            "test", recent_window=10, llm_summarize=True, char_budget=50000
        )

        assert "Cached summary." in result[0]["content"]

    async def test_stale_cache_invalidated(self, tmp_path):
        """Stale cache (watermark mismatch) triggers new LLM call."""
        entries = _make_entries(20)
        store = AsyncMock()
        store.get_session = AsyncMock(return_value=entries)
        store.sessions_path = tmp_path

        # Write stale cache
        cache_file = tmp_path / "test_compaction.json"
        cache_file.write_text(
            json.dumps({"watermark": 15, "summary": "Old summary.", "older_count": 5})
        )

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Fresh summary.")]

        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_cls.return_value = mock_client

            mgr = MemoryManager(store=store)
            result = await mgr.get_compacted_history(
                "test", recent_window=10, llm_summarize=True, char_budget=50000
            )

        assert "Fresh summary." in result[0]["content"]
        # Cache should be updated
        cache = json.loads(cache_file.read_text())
        assert cache["watermark"] == 20

    async def test_no_sessions_path_falls_back(self):
        """Mem0 backend (no sessions_path) falls back to Tier 1."""
        entries = _make_entries(15)
        mgr = _make_manager(entries, has_sessions_path=False)

        result = await mgr.get_compacted_history(
            "test", recent_window=10, llm_summarize=True, char_budget=50000
        )

        # Should still work with Tier 1 extracts
        assert result[0]["content"].startswith("[Earlier conversation]")
        assert "User:" in result[0]["content"]


# ─── Backward compatibility ─────────────────────────────────────────────


class TestBackwardCompat:
    async def test_get_session_history_unchanged(self):
        """get_session_history() still works as before."""
        entries = _make_entries(25)
        mgr = _make_manager(entries)
        result = await mgr.get_session_history("test", limit=20)
        assert len(result) == 20
        # Should be the last 20 entries
        assert result[-1]["content"] == entries[-1].content
