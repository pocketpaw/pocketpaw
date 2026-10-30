# tests/cloud/test_multitenant_session_isolation.py
# Created 2026-06-30 (feat/session-supervisor SS-7) — the durable (Mongo) half of
# the consolidated, ADVERSARIAL multi-tenant session-isolation GATE. The OSS half
# (shared-backing store, supervisor quota, SDK resume guards) lives in
# tests/test_multitenant_session_isolation.py; this file proves the same isolation
# holds on the REAL Beanie query path (mongomock-motor — no live mongod), where
# every tenant's rows physically share ONE collection.
#
# Covers two SS-7 concerns that need persistence:
#   1. MongoSessionStore isolation over one collection — a workspace-B store can
#      neither load, list, nor delete a workspace-A session that collides on the
#      same (project_key, session_id); a both-write collision keeps two scoped rows.
#   2. Durable (workspace, session_id, agent_id) -> cli_session_id mapping
#      isolation — a B lookup for an (id, agent) only A wrote is None, and a B
#      write for the SAME (session, agent) creates a SEPARATE row (the unique index
#      leads on ``workspace``) instead of overwriting A's mapping.
#
# These are the ADVERSARIAL cross-tenant scenarios; the per-slice happy-path
# tenancy tests (SS-2 / SS-3) live in test_session_transcript_store.py and
# test_agent_session_runtime_service.py and are not duplicated here.

from __future__ import annotations

from pocketpaw_ee.cloud.agent_sessions import runtime_service
from pocketpaw_ee.cloud.agent_sessions.store import MongoSessionStore
from pocketpaw_ee.cloud.models.agent_session_runtime import AgentSessionRuntimeDoc


def _key(project_key="proj", session_id="sess-1", subpath=None):
    k = {"project_key": project_key, "session_id": session_id}
    if subpath is not None:
        k["subpath"] = subpath
    return k


# ===========================================================================
# 1. MongoSessionStore isolation over one collection
# ===========================================================================


async def test_mongo_store_tenant_b_cannot_observe_or_delete_tenant_a(mongo_db) -> None:  # noqa: ARG001
    """One collection, two tenants colliding on the SAME (project_key, session_id).
    A wrote it; B never did. B's load is None, B's listings exclude A, a guessed-id
    load is None, and B deleting "its" key leaves A's row intact — the workspace
    filter on every query is the only thing between the tenants and it holds."""
    store_a = MongoSessionStore("ws-A")
    await store_a.append(_key(), [{"type": "user", "uuid": "a1"}])
    await store_a.append(_key(subpath="subagents/agent-1"), [{"type": "user", "uuid": "as1"}])

    store_b = MongoSessionStore("ws-B")
    assert await store_b.load(_key()) is None
    assert await store_b.list_sessions("proj") == []
    assert await store_b.list_subkeys(_key()) == []
    # "Guessed id" attack: a session_id that exists ONLY under A.
    assert await store_b.load(_key(session_id="sess-1")) is None

    # B deleting that key must not cascade into A's rows.
    await store_b.delete(_key())
    assert await store_a.load(_key()) == [{"type": "user", "uuid": "a1"}]
    assert await store_a.list_subkeys(_key()) == ["subagents/agent-1"]


async def test_mongo_store_same_key_two_tenants_coexist_and_delete_is_scoped(mongo_db) -> None:  # noqa: ARG001
    """Both tenants write the SAME (project_key, session_id) into one collection.
    Two ``workspace``-scoped rows coexist; each loads ITS OWN content, and B's
    delete removes only B's row while A's survives."""
    store_a = MongoSessionStore("ws-A")
    store_b = MongoSessionStore("ws-B")
    await store_a.append(_key(), [{"type": "user", "uuid": "a-row"}])
    await store_b.append(_key(), [{"type": "user", "uuid": "b-row"}])

    # Each tenant sees only its own write — B's append did not clobber A's row.
    assert await store_a.load(_key()) == [{"type": "user", "uuid": "a-row"}]
    assert await store_b.load(_key()) == [{"type": "user", "uuid": "b-row"}]

    await store_b.delete(_key())
    assert await store_b.load(_key()) is None
    assert await store_a.load(_key()) == [{"type": "user", "uuid": "a-row"}]


# ===========================================================================
# 2. Durable (workspace, session, agent) -> cli_session_id mapping isolation
# ===========================================================================


async def test_runtime_mapping_b_write_creates_separate_row_not_an_overwrite(mongo_db) -> None:  # noqa: ARG001
    """The unique index leads on ``workspace``, so a B write for the SAME
    (session_id, agent_id) A already mapped creates a SEPARATE row instead of
    upserting A's. A leaked/guessed (session, agent) from another tenant can never
    collide with — or overwrite — A's native cli_session_id."""
    await runtime_service.set_cli_session_id("ws-A", "sess", "agent", "cli-A")

    # B has no mapping for the same (session, agent) A wrote.
    assert await runtime_service.get_cli_session_id("ws-B", "sess", "agent") is None

    # B writes the SAME (session, agent) → a new row keyed on ws-B, NOT an
    # overwrite of A's row.
    await runtime_service.set_cli_session_id("ws-B", "sess", "agent", "cli-B")
    assert await runtime_service.get_cli_session_id("ws-A", "sess", "agent") == "cli-A"
    assert await runtime_service.get_cli_session_id("ws-B", "sess", "agent") == "cli-B"

    # Exactly two rows exist for this (session, agent) — one per workspace —
    # proving the workspace-led unique index kept them apart.
    rows = await AgentSessionRuntimeDoc.find(
        AgentSessionRuntimeDoc.session_id == "sess",
        AgentSessionRuntimeDoc.agent_id == "agent",
    ).to_list()
    assert len(rows) == 2
    assert {r.workspace for r in rows} == {"ws-A", "ws-B"}
    assert {r.cli_session_id for r in rows} == {"cli-A", "cli-B"}
