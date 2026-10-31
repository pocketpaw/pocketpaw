# tests/instinct/test_expired_status.py — an Action the decisions abandon-path
# sweeper flipped to ``expired`` (raw UPDATE, see
# ee/pocketpaw_ee/cloud/decisions/_action_sweeper.py) must read back through
# the store. One such row used to make every ``list_actions`` call raise
# ``ValueError: 'expired' is not a valid ActionStatus``. OSS store only.

from __future__ import annotations

import aiosqlite
import pytest

from pocketpaw.instinct.models import ActionStatus, ActionTrigger
from pocketpaw.instinct.store import InstinctStore


async def _propose(store: InstinctStore, title: str):
    return await store.propose(
        pocket_id="p1",
        title=title,
        description="",
        recommendation="rec",
        trigger=ActionTrigger(type="agent", source="tester", reason="expired test"),
    )


@pytest.mark.asyncio
async def test_list_actions_reads_expired_row(tmp_path):
    store = InstinctStore(tmp_path / "instinct.db")
    stale = await _propose(store, "parked write")
    live = await _propose(store, "still pending")
    # Same write the sweeper does.
    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE instinct_actions SET status = ? WHERE id = ?", ("expired", stale.id)
        )
        await db.commit()

    actions = {a.id: a for a in await store.list_actions(pocket_id="p1")}

    assert actions[stale.id].status == ActionStatus.EXPIRED
    assert actions[live.id].status == ActionStatus.PENDING
    expired = await store.list_actions(status=ActionStatus.EXPIRED)
    assert [a.id for a in expired] == [stale.id]
