# Daily-notes cross-user isolation tests.
# Added: 2026-04-16 for security sprint cluster D (#887).

from __future__ import annotations

import pytest


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """Fresh MemoryManager backed by a tmp FileMemoryStore.

    Forces a non-default owner_id so _resolve_user_id() gives alice and bob
    distinct scoped IDs — otherwise both resolve to "default" and the
    isolation test is a no-op.
    """
    from pocketpaw.config import get_settings
    from pocketpaw.memory.file_store import FileMemoryStore
    from pocketpaw.memory.manager import MemoryManager

    s = get_settings()
    monkeypatch.setattr(s, "owner_id", "owner")

    store = FileMemoryStore(tmp_path / "memory")
    return MemoryManager(store)


class TestDailyNotesIsolation:
    """Daily notes must be scoped to sender_id — user A never sees user B's notes."""

    async def test_note_records_sender_id(self, manager):
        from pocketpaw.memory.protocol import MemoryType

        expected_uid = manager._resolve_user_id("alice")
        note_id = await manager.note("alice's groceries list", sender_id="alice")

        entries = await manager._store.get_by_type(MemoryType.DAILY, limit=100)
        assert any(
            e.id == note_id and e.metadata.get("user_id") == expected_uid for e in entries
        ), "daily note must carry a user_id derived from sender_id"

    async def test_context_excludes_other_users_daily_notes(self, manager):
        await manager.note("alice-secret-note", sender_id="alice")
        await manager.note("bob-secret-note", sender_id="bob")

        alice_ctx = await manager.get_context_for_agent(sender_id="alice")
        bob_ctx = await manager.get_context_for_agent(sender_id="bob")

        assert "alice-secret-note" in alice_ctx
        assert "bob-secret-note" not in alice_ctx, (
            "Cross-user daily-note leak: alice saw bob's note"
        )
        assert "bob-secret-note" in bob_ctx
        assert "alice-secret-note" not in bob_ctx

    async def test_legacy_notes_without_sender_id_are_visible(self, manager):
        """Daily notes written before the fix have no user_id metadata.

        Treat them as system-wide (backward-compat) so operators don't
        lose visibility into historical notes when they upgrade.
        """
        from pocketpaw.memory.manager import MemoryEntry, MemoryType

        legacy = MemoryEntry(
            id="",
            type=MemoryType.DAILY,
            content="legacy-shared-note",
            tags=[],
            metadata={},  # no user_id
        )
        await manager._store.save(legacy)

        alice_ctx = await manager.get_context_for_agent(sender_id="alice")
        assert "legacy-shared-note" in alice_ctx
