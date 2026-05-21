"""MongoMemoryStore — multi-tenant workspace isolation."""

from __future__ import annotations

import uuid

from pocketpaw_ee.cloud.models.session import Session

from pocketpaw.memory.protocol import MemoryEntry, MemoryType


def _entry(session_key: str, role: str, content: str) -> MemoryEntry:
    return MemoryEntry(
        id="",
        type=MemoryType.SESSION,
        content=content,
        role=role,
        session_key=session_key,
    )


class TestSessionStamping:
    """SESSION writes auto-resolve workspace_id from the linked Session row."""

    async def test_session_save_stamps_workspace_from_metadata(self, store):
        key = f"sess-{uuid.uuid4().hex[:8]}"
        entry = MemoryEntry(
            id="",
            type=MemoryType.SESSION,
            content="explicit",
            role="user",
            session_key=key,
            metadata={"workspace_id": "ws-explicit"},
        )
        await store.save(entry)

        from pocketpaw_ee.cloud.models.message import Message

        rows = await Message.find({"session_key": key}).to_list()
        assert len(rows) == 1
        assert rows[0].workspace_id == "ws-explicit"

    async def test_session_save_resolves_workspace_from_session_doc(self, store):
        key = f"sess-{uuid.uuid4().hex[:8]}"
        await Session(
            sessionId=key,
            context_type="pocket",
            workspace="ws-from-session",
            owner="u1",
        ).insert()

        await store.save(_entry(key, "user", "implicit"))

        from pocketpaw_ee.cloud.models.message import Message

        rows = await Message.find({"session_key": key}).to_list()
        assert rows[0].workspace_id == "ws-from-session"

    async def test_session_save_leaves_workspace_none_when_unresolvable(self, store):
        # No Session doc, no metadata — adapter persists with workspace_id=None.
        # Tenant-scoped reads will not return this row.
        key = f"sess-{uuid.uuid4().hex[:8]}"
        await store.save(_entry(key, "user", "untagged"))

        from pocketpaw_ee.cloud.models.message import Message

        rows = await Message.find({"session_key": key}).to_list()
        assert rows[0].workspace_id is None


class TestSessionWorkspaceReads:
    async def test_get_session_in_workspace_filters_other_tenants(self, store):
        # Two sessions, same key prefix, different workspaces. Even though
        # session_key uniqueness already prevents collisions in practice
        # (Session.sessionId is unique), the explicit workspace filter is
        # the defence-in-depth the reviewer asked for.
        key_a = f"sess-A-{uuid.uuid4().hex[:6]}"
        key_b = f"sess-B-{uuid.uuid4().hex[:6]}"
        await Session(sessionId=key_a, context_type="pocket", workspace="ws-A", owner="u1").insert()
        await Session(sessionId=key_b, context_type="pocket", workspace="ws-B", owner="u2").insert()

        await store.save(_entry(key_a, "user", "from A"))
        await store.save(_entry(key_b, "user", "from B"))

        a_in_a = await store.get_session_in_workspace(key_a, "ws-A")
        a_in_b = await store.get_session_in_workspace(key_a, "ws-B")

        assert [m.content for m in a_in_a] == ["from A"]
        assert a_in_b == [], "wrong-workspace lookup must return nothing"

    async def test_get_session_in_workspace_excludes_untagged_rows(self, store):
        """Untagged rows (no workspace_id) must NOT match a workspace query."""
        key = f"sess-{uuid.uuid4().hex[:8]}"
        await store.save(_entry(key, "user", "untagged"))

        got = await store.get_session_in_workspace(key, "ws-X")
        assert got == []


class TestFactStamping:
    async def test_fact_save_stamps_workspace_from_metadata(self, store):
        entry = MemoryEntry(
            id="",
            type=MemoryType.LONG_TERM,
            content="user prefers dark mode",
            tags=["prefs"],
            metadata={"user_id": "u1", "workspace_id": "ws-1"},
        )
        await store.save(entry)

        from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc

        rows = await MemoryFactDoc.find({"content": "user prefers dark mode"}).to_list()
        assert rows[0].workspace_id == "ws-1"
        assert rows[0].user_id == "u1"
        assert "workspace_id" not in rows[0].metadata
        assert "user_id" not in rows[0].metadata


class TestFactWorkspaceReads:
    async def test_list_facts_in_workspace_filters_other_tenants(self, store):
        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="A fact",
                metadata={"workspace_id": "ws-A"},
            )
        )
        await store.save(
            MemoryEntry(
                id="",
                type=MemoryType.LONG_TERM,
                content="B fact",
                metadata={"workspace_id": "ws-B"},
            )
        )
        await store.save(MemoryEntry(id="", type=MemoryType.LONG_TERM, content="legacy untagged"))

        a_facts = await store.list_facts_in_workspace("ws-A")
        b_facts = await store.list_facts_in_workspace("ws-B")
        contents_a = [f.content for f in a_facts]
        contents_b = [f.content for f in b_facts]

        assert "A fact" in contents_a and "B fact" not in contents_a
        assert "B fact" in contents_b and "A fact" not in contents_b
        # The untagged legacy row must not leak into either workspace.
        assert "legacy untagged" not in contents_a
        assert "legacy untagged" not in contents_b
