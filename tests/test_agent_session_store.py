# tests/test_agent_session_store.py
# Created: 2026-06-30 (feat/session-supervisor SS-2) — pins the contract for the
# tenancy-keyed SessionStore and its claude_sdk wiring. Two concerns:
#
#   1. The OSS reference ``InMemorySessionStore`` satisfies the SDK's
#      ``SessionStore`` protocol and is correctly tenancy-keyed:
#        * round-trip — append -> load returns the entries; list_sessions
#          includes the session; list_subkeys finds a subagent subpath.
#        * durability across a "restart" — a FRESH store instance over the SAME
#          backing dict still loads prior writes (durability lives in the store,
#          not the process).
#        * tenancy isolation — a store bound to workspace B can NEVER read a
#          session written by a store bound to workspace A (the core property
#          SS-7 will test hard).
#        * idempotency + cascade-delete edge cases.
#
#   2. The claude_sdk wiring SPY (no live model call — blocked in this env):
#      when ``run()`` gets a ``SessionHandle(session_store=<obj>)``, the
#      constructed ``ClaudeAgentOptions`` carries that exact ``session_store``;
#      a handle without a store leaves it unset (SS-1 / legacy behavior).
#
# The spy reuses the proven SS-1 run()-driving harness
# (``tests.test_claude_sdk_session_handle``) so a full ``run`` completes WITHOUT
# a live call and the constructed options are observable.

from __future__ import annotations

import pytest

from pocketpaw.agents.backend import SessionHandle
from pocketpaw.agents.session_store import InMemorySessionStore

# Reuse the SS-1 harness (capturing options factory + faked SDK stream).
from tests.test_claude_sdk_session_handle import _drive_run, _make_sdk

# ===========================================================================
# OSS reference store — protocol conformance + tenancy keying
# ===========================================================================


def _key(project_key="proj", session_id="sess-1", subpath=None):
    k = {"project_key": project_key, "session_id": session_id}
    if subpath is not None:
        k["subpath"] = subpath
    return k


async def test_round_trip_append_load_list_sessions_and_subkeys() -> None:
    """append -> load round-trips; list_sessions includes the session; a
    subagent subpath is discoverable via list_subkeys and excluded from
    list_sessions."""
    store = InMemorySessionStore("ws-A")
    e1 = {"type": "user", "uuid": "u1", "timestamp": "t1"}
    e2 = {"type": "assistant", "uuid": "u2", "timestamp": "t2"}

    await store.append(_key(), [e1])
    await store.append(_key(), [e2])

    assert await store.load(_key()) == [e1, e2]

    sessions = await store.list_sessions("proj")
    assert [s["session_id"] for s in sessions] == ["sess-1"]
    assert isinstance(sessions[0]["mtime"], int)

    # A subagent transcript rides a subpath; it's a separate row.
    sub = {"type": "user", "uuid": "s1"}
    await store.append(_key(subpath="subagents/agent-7"), [sub])
    assert await store.load(_key(subpath="subagents/agent-7")) == [sub]
    assert await store.list_subkeys(_key()) == ["subagents/agent-7"]

    # list_sessions returns only MAIN transcripts — the subpath row must not
    # leak in as a second "session".
    sessions2 = await store.list_sessions("proj")
    assert [s["session_id"] for s in sessions2] == ["sess-1"]


async def test_load_returns_none_for_never_written_key() -> None:
    store = InMemorySessionStore("ws-A")
    assert await store.load(_key(session_id="nope")) is None


async def test_append_dedups_by_uuid() -> None:
    """An entry carrying a ``uuid`` already present is ignored (the protocol's
    idempotency contract); a uuid-less entry is always appended."""
    store = InMemorySessionStore("ws-A")
    e = {"type": "user", "uuid": "dup"}
    await store.append(_key(), [e])
    await store.append(_key(), [e])  # same uuid → ignored
    no_uuid = {"type": "summary"}
    await store.append(_key(), [no_uuid])
    await store.append(_key(), [no_uuid])  # no uuid → appended twice
    loaded = await store.load(_key())
    assert loaded == [e, no_uuid, no_uuid]


async def test_durability_across_restart_via_shared_backing() -> None:
    """A FRESH store instance over the SAME backing dict still loads prior
    writes — proving durability lives in the backing store, not the instance
    (the in-memory analog of resuming from Mongo after a backend restart)."""
    shared: dict = {}
    store1 = InMemorySessionStore("ws-A", backing=shared)
    entries = [{"type": "user", "uuid": "u1"}, {"type": "assistant", "uuid": "u2"}]
    await store1.append(_key(), entries)

    # Simulate the restart: a brand-new instance, same backing store.
    store2 = InMemorySessionStore("ws-A", backing=shared)
    assert await store2.load(_key()) == entries


async def test_tenancy_isolation_other_workspace_sees_nothing() -> None:
    """A store bound to workspace B can NEVER observe a session written under
    workspace A — even over the same physical backing store. Core property."""
    shared: dict = {}
    store_a = InMemorySessionStore("ws-A", backing=shared)
    await store_a.append(_key(), [{"type": "user", "uuid": "u1"}])
    await store_a.append(_key(subpath="subagents/agent-1"), [{"type": "user", "uuid": "s1"}])

    store_b = InMemorySessionStore("ws-B", backing=shared)
    assert await store_b.load(_key()) is None
    assert await store_b.list_sessions("proj") == []
    assert await store_b.list_subkeys(_key()) == []

    # And A still sees its own data (the namespace didn't clobber it).
    assert await store_a.load(_key()) == [{"type": "user", "uuid": "u1"}]


async def test_delete_main_cascades_to_subkeys() -> None:
    store = InMemorySessionStore("ws-A")
    await store.append(_key(), [{"type": "user", "uuid": "u1"}])
    await store.append(_key(subpath="subagents/agent-1"), [{"type": "user", "uuid": "s1"}])

    # Deleting the main transcript (no subpath) cascades to subagent rows.
    await store.delete(_key())
    assert await store.load(_key()) is None
    assert await store.load(_key(subpath="subagents/agent-1")) is None
    assert await store.list_subkeys(_key()) == []


async def test_delete_targeted_subpath_only() -> None:
    store = InMemorySessionStore("ws-A")
    await store.append(_key(), [{"type": "user", "uuid": "u1"}])
    await store.append(_key(subpath="subagents/agent-1"), [{"type": "user", "uuid": "s1"}])

    await store.delete(_key(subpath="subagents/agent-1"))
    # Only the subagent row went; the main transcript survives.
    assert await store.load(_key()) == [{"type": "user", "uuid": "u1"}]
    assert await store.list_subkeys(_key()) == []


def test_workspace_id_is_required() -> None:
    with pytest.raises(ValueError):
        InMemorySessionStore("")


# ===========================================================================
# claude_sdk wiring spy — options carry the session_store opaquely
# ===========================================================================


async def test_session_store_is_threaded_into_constructed_options() -> None:
    """A run with ``SessionHandle(session_store=<obj>)`` builds
    ``ClaudeAgentOptions`` carrying that exact object — proving claude_sdk
    forwards the store opaquely so the SDK can materialize a resume from it."""
    options_sink: list = []
    stateless_options: list = []
    sdk = _make_sdk(options_sink, stateless_options)

    sentinel = object()
    handle = SessionHandle(cli_session_id=None, session_store=sentinel)
    events = await _drive_run(sdk, "turn", session_handle=handle)

    assert any(e.type == "done" for e in events)
    assert options_sink, "options must have been constructed"
    assert getattr(options_sink[-1], "session_store", None) is sentinel, (
        "the constructed ClaudeAgentOptions must carry the handle's session_store "
        "so the SDK materializes a resume from OUR store, not local disk"
    )


async def test_no_store_leaves_session_store_unset() -> None:
    """A handle WITHOUT a store (or no handle) leaves ``session_store`` unset on
    the options — the unchanged SS-1 / legacy path."""
    options_sink: list = []
    stateless_options: list = []
    sdk = _make_sdk(options_sink, stateless_options)

    await _drive_run(sdk, "turn", session_handle=SessionHandle(session_store=None))
    assert getattr(options_sink[-1], "session_store", None) is None

    await _drive_run(sdk, "turn2", session_handle=None)
    assert getattr(options_sink[-1], "session_store", None) is None
