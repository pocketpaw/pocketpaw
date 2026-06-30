# tests/cloud/test_session_transcript_store.py
# Created: 2026-06-30 (feat/session-supervisor SS-2) — proves the Mongo-backed
# MongoSessionStore mirrors the OSS in-memory reference impl's semantics on a
# real Beanie query path (mongomock-motor — no live mongod). Same three core
# assertions as the OSS store: round-trip + list_sessions/list_subkeys,
# durability across a "restart" (a fresh adapter instance over the same DB),
# and tenancy isolation (a store bound to workspace B can't read workspace A's
# session). Uses the shared ``mongo_db`` fixture which init_beanie's
# ALL_DOCUMENTS (now including SessionTranscriptDoc).

from __future__ import annotations

from pocketpaw_ee.cloud.agent_sessions.store import MongoSessionStore


def _key(project_key="proj", session_id="sess-1", subpath=None):
    k = {"project_key": project_key, "session_id": session_id}
    if subpath is not None:
        k["subpath"] = subpath
    return k


async def test_round_trip_and_listings(mongo_db) -> None:  # noqa: ARG001 — forces Beanie init
    store = MongoSessionStore("ws-A")
    e1 = {"type": "user", "uuid": "u1", "timestamp": "t1"}
    e2 = {"type": "assistant", "uuid": "u2", "timestamp": "t2"}

    await store.append(_key(), [e1])
    await store.append(_key(), [e2])
    assert await store.load(_key()) == [e1, e2]

    sessions = await store.list_sessions("proj")
    assert [s["session_id"] for s in sessions] == ["sess-1"]
    assert isinstance(sessions[0]["mtime"], int)

    sub = {"type": "user", "uuid": "s1"}
    await store.append(_key(subpath="subagents/agent-7"), [sub])
    assert await store.load(_key(subpath="subagents/agent-7")) == [sub]
    assert await store.list_subkeys(_key()) == ["subagents/agent-7"]
    # The subagent row must not surface as a second main session.
    assert [s["session_id"] for s in await store.list_sessions("proj")] == ["sess-1"]


async def test_append_dedups_by_uuid(mongo_db) -> None:  # noqa: ARG001
    store = MongoSessionStore("ws-A")
    e = {"type": "user", "uuid": "dup"}
    await store.append(_key(), [e])
    await store.append(_key(), [e])
    no_uuid = {"type": "summary"}
    await store.append(_key(), [no_uuid])
    await store.append(_key(), [no_uuid])
    assert await store.load(_key()) == [e, no_uuid, no_uuid]


async def test_durability_across_fresh_instance(mongo_db) -> None:  # noqa: ARG001
    """A FRESH adapter instance over the SAME DB still loads prior writes —
    durability lives in Mongo, not the process (the real restart scenario)."""
    entries = [{"type": "user", "uuid": "u1"}, {"type": "assistant", "uuid": "u2"}]
    await MongoSessionStore("ws-A").append(_key(), entries)
    # New instance, same backing collection.
    assert await MongoSessionStore("ws-A").load(_key()) == entries


async def test_tenancy_isolation(mongo_db) -> None:  # noqa: ARG001
    """A store bound to workspace B can't read a session written under
    workspace A, even sharing the same collection."""
    await MongoSessionStore("ws-A").append(_key(), [{"type": "user", "uuid": "u1"}])
    await MongoSessionStore("ws-A").append(
        _key(subpath="subagents/agent-1"), [{"type": "user", "uuid": "s1"}]
    )

    store_b = MongoSessionStore("ws-B")
    assert await store_b.load(_key()) is None
    assert await store_b.list_sessions("proj") == []
    assert await store_b.list_subkeys(_key()) == []

    # A still sees its own row.
    assert await MongoSessionStore("ws-A").load(_key()) == [{"type": "user", "uuid": "u1"}]


async def test_delete_main_cascades(mongo_db) -> None:  # noqa: ARG001
    store = MongoSessionStore("ws-A")
    await store.append(_key(), [{"type": "user", "uuid": "u1"}])
    await store.append(_key(subpath="subagents/agent-1"), [{"type": "user", "uuid": "s1"}])

    await store.delete(_key())
    assert await store.load(_key()) is None
    assert await store.load(_key(subpath="subagents/agent-1")) is None
    assert await store.list_subkeys(_key()) == []


async def test_delete_targeted_subpath(mongo_db) -> None:  # noqa: ARG001
    store = MongoSessionStore("ws-A")
    await store.append(_key(), [{"type": "user", "uuid": "u1"}])
    await store.append(_key(subpath="subagents/agent-1"), [{"type": "user", "uuid": "s1"}])

    await store.delete(_key(subpath="subagents/agent-1"))
    assert await store.load(_key()) == [{"type": "user", "uuid": "u1"}]
    assert await store.list_subkeys(_key()) == []
