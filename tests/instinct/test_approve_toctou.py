# tests/instinct/test_approve_toctou.py — NEW (2026-07-05,
#   fix/atlas-admin-security-hardening, FINDING B).
# Store-level coverage that InstinctStore._update_status flips status ATOMICALLY:
# the UPDATE's WHERE now carries ``AND status = ?`` (the required status), so two
# concurrent approve() calls on the same PENDING action cannot BOTH win. Exactly
# one returns the Action; the loser returns None (rowcount==0). This closes the
# TOCTOU where the Python-side require_status pre-check (after get_action) let two
# approvers both read PENDING and both flip to APPROVED, double-firing the admin
# executor. Exercises the OSS store directly (no EE import), matching the
# test_update_status_atomic.py convention.

from __future__ import annotations

import asyncio

from pocketpaw.instinct.models import ActionStatus, ActionTrigger
from pocketpaw.instinct.store import InstinctStore


def _trigger() -> ActionTrigger:
    return ActionTrigger(type="agent", source="tester", reason="toctou test")


async def _propose(store: InstinctStore, *, pocket_id="p1"):
    return await store.propose(
        pocket_id=pocket_id,
        title="do a thing",
        description="",
        recommendation="rec",
        trigger=_trigger(),
    )


async def test_two_concurrent_approves_exactly_one_wins(tmp_path):
    """Two interleaved approve() calls on ONE pending action: exactly one
    returns the Action, the other returns None.

    Both are forced to observe the PENDING status (the require_status pre-check
    passes for both) before either UPDATE lands — the classic TOCTOU. Only the
    atomic ``AND status = ?`` in the UPDATE's WHERE can make the second flip a
    no-op. Without the fix BOTH return a non-None Action and the executor
    double-fires.
    """
    store = InstinctStore(tmp_path / "instinct.db")
    action = await _propose(store)
    assert action.status == ActionStatus.PENDING

    real_get_action = store.get_action
    # A barrier both coroutines pass through AFTER their initial get_action but
    # BEFORE their UPDATE, guaranteeing both saw PENDING (the check-then-act race).
    both_read = asyncio.Event()
    read_count = {"n": 0}

    async def _gated_get_action(action_id: str):
        result = await real_get_action(action_id)
        # Only gate the FIRST read inside each _update_status (the require_status
        # check). The trailing get_action (return value reload) must not block.
        if read_count["n"] < 2:
            read_count["n"] += 1
            if read_count["n"] == 2:
                both_read.set()
            await both_read.wait()
        return result

    store.get_action = _gated_get_action  # type: ignore[method-assign]

    results = await asyncio.gather(
        store.approve(action.id, approver="alice"),
        store.approve(action.id, approver="bob"),
    )

    non_none = [r for r in results if r is not None]
    assert len(non_none) == 1, (
        f"expected exactly one approve() to win, got {len(non_none)} "
        f"(TOCTOU double-fire): {results!r}"
    )

    # The action is APPROVED exactly once and stays there.
    reloaded = await real_get_action(action.id)
    assert reloaded is not None
    assert reloaded.status == ActionStatus.APPROVED


async def test_reapprove_after_flip_is_noop(tmp_path):
    """A serial re-approve of an already-APPROVED action returns None — the
    atomic WHERE (not just the Python pre-check) enforces it."""
    store = InstinctStore(tmp_path / "instinct.db")
    action = await _propose(store)

    first = await store.approve(action.id, approver="alice")
    assert first is not None
    assert first.status == ActionStatus.APPROVED

    second = await store.approve(action.id, approver="bob")
    assert second is None
