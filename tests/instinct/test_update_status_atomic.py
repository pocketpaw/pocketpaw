# tests/instinct/test_update_status_atomic.py — NEW (2026-06-10, sov/r2a FIX 2).
# Store-level coverage that InstinctStore._update_status is ATOMIC with its
# audit write: if the audit-chain append raises, the action status flip is
# rolled back (no orphaned status-without-audit row), and the W2b chain stays
# intact / verifiable on the happy path. Exercises the OSS store directly
# (no EE import), matching the test_w4a_migration.py convention.

from __future__ import annotations

import pytest

from pocketpaw.instinct.models import ActionStatus, ActionTrigger
from pocketpaw.instinct.store import AuditChainError, InstinctStore


def _trigger() -> ActionTrigger:
    return ActionTrigger(type="agent", source="tester", reason="atomic update test")


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
async def test_update_status_rolls_back_when_audit_write_fails(tmp_path):
    """If the audit-chain append raises, the action status flip is rolled back
    — no action ends up approved/rejected/executed without an audit row."""
    store = InstinctStore(tmp_path / "instinct.db")
    action = await _propose(store)
    assert action.status == ActionStatus.PENDING

    # Count audit rows before the failed approve so we can prove none leaked.
    before = await store.query_audit(limit=10000)

    # Force the in-transaction audit append to fail.
    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated chain write failure")

    store._append_audit_locked = _boom  # type: ignore[assignment]

    with pytest.raises(AuditChainError):
        await store.approve(action.id, approver="alice")

    # The status flip must have rolled back: still PENDING.
    reloaded = await store.get_action(action.id)
    assert reloaded is not None
    assert reloaded.status == ActionStatus.PENDING
    assert reloaded.approved_by is None

    # And no partial audit row leaked.
    after = await store.query_audit(limit=10000)
    assert len(after) == len(before)


@pytest.mark.asyncio
async def test_update_status_commits_both_on_success_and_chain_verifies(tmp_path):
    """The happy path still flips the status AND writes the audit row, and the
    W2b hash chain remains intact and verifiable after the atomic write."""
    store = InstinctStore(tmp_path / "instinct.db")
    action = await _propose(store)

    approved = await store.approve(action.id, approver="alice")
    assert approved is not None
    assert approved.status == ActionStatus.APPROVED
    assert approved.approved_by == "alice"

    # The approve wrote a new audit row (event=action_approved) atomically with
    # the status flip.
    audit = await store.query_audit(limit=10000)
    events = [e.event for e in audit if e.action_id == action.id]
    assert "action_proposed" in events
    assert "action_approved" in events

    # The tamper-evident chain still verifies end to end.
    verdict = await store.verify_audit_chain()
    assert verdict["intact"] is True
    assert verdict["hashed"] >= 2
