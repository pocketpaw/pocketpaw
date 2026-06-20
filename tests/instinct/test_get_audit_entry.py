# tests/instinct/test_get_audit_entry.py — NEW (2026-06-10, sov/r2a FIX 1).
# Store-level coverage for InstinctStore.get_audit_entry(audit_id,
# workspace_id=None): a direct single-row lookup by id. Proves an audit entry
# OLDER than the previous 1000-row router window is still retrievable, and that
# a cross-workspace id is not visible under a scoped read (the W4a scope
# applies). Exercises the OSS store directly (no EE import), matching the
# test_w4a_migration.py convention.

from __future__ import annotations

import pytest

from pocketpaw.instinct.models import ActionTrigger
from pocketpaw.instinct.store import InstinctStore


def _trigger() -> ActionTrigger:
    return ActionTrigger(type="agent", source="tester", reason="get_audit_entry test")


async def _propose(store: InstinctStore, *, pocket_id="p1", workspace_id=None):
    return await store.propose(
        pocket_id=pocket_id,
        title="do a thing",
        description="",
        recommendation="rec",
        trigger=_trigger(),
        workspace_id=workspace_id,
    )


@pytest.mark.asyncio
async def test_get_audit_entry_returns_entry_older_than_1000_rows(tmp_path):
    """An audit entry written FIRST (so it is far older than the most recent
    1000) is still retrievable by id. The previous router path paged the most
    recent 1000 rows and matched in Python, 404-ing on this exact case."""
    store = InstinctStore(tmp_path / "instinct.db")

    # The first proposal's audit row is the OLDEST in the ledger.
    first = await _propose(store)
    # query_audit is newest-first (DESC), so the last element is the oldest row.
    all_entries = await store.query_audit(limit=10000)
    oldest_id = all_entries[-1].id

    # Bury it under > 1000 newer audit rows via standalone log() writes — far
    # cheaper than proposing 1000 actions and exercises the same ledger.
    for i in range(1100):
        await store.log(actor="system", event="noise", description=f"n{i}")

    # The old router window (limit=1000, newest-first) would NOT include the
    # oldest row. The direct lookup must still find it.
    entry = await store.get_audit_entry(oldest_id)
    assert entry is not None
    assert entry.id == oldest_id
    assert entry.action_id == first.id


@pytest.mark.asyncio
async def test_get_audit_entry_scopes_by_workspace(tmp_path):
    """A scoped read sees its own workspace's rows + legacy NULL rows, but NOT
    another workspace's id (returns None, never leaking existence)."""
    store = InstinctStore(tmp_path / "instinct.db")

    a = await _propose(store, workspace_id="ws-a")
    b = await _propose(store, workspace_id="ws-b")

    # Find each action's proposal audit row id.
    audit = await store.query_audit(limit=10000)
    by_action = {e.action_id: e.id for e in audit if e.event == "action_proposed"}
    a_id = by_action[a.id]
    b_id = by_action[b.id]

    # Own workspace: visible.
    assert (await store.get_audit_entry(a_id, workspace_id="ws-a")) is not None
    # Cross-workspace: NOT visible under a scoped read.
    assert (await store.get_audit_entry(b_id, workspace_id="ws-a")) is None
    # Unscoped (OSS): everything visible.
    assert (await store.get_audit_entry(b_id)) is not None


@pytest.mark.asyncio
async def test_get_audit_entry_unknown_id_is_none(tmp_path):
    store = InstinctStore(tmp_path / "instinct.db")
    await _propose(store)
    assert (await store.get_audit_entry("does-not-exist")) is None
