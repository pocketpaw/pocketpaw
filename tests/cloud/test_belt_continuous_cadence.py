# tests/cloud/test_belt_continuous_cadence.py — the ``continuous`` cadence.
#
# A continuous mandate is due when it is active, its latest shift is finished
# (``done``, or ``stood_down`` with a sighting filed or a run settled since:
# landed, rejected, released or failed), the line has headroom under its
# in-flight cap (a run with no store write for the stall timeout no longer
# counts), it has an open sighting no task is working, and the minimum gap
# since the last shift started has passed. A shift stuck in ``planning`` or
# ``executing`` is released by the stall timeout; one waiting ``in_gate`` for a
# human never is. Overlapping shifts and the shift budget have their own file
# (``test_belt_overlapping_shifts.py``). Also pinned: the scheduler loop ticks at the continuous
# interval while any continuous mandate exists, and the hourly one otherwise.
# The clock is injected; the instinct store is an isolated file.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.cloud.mandates import scheduler as scheduler_mod  # noqa: E402
from pocketpaw_ee.cloud.mandates import service as mandate_service  # noqa: E402
from pocketpaw_ee.cloud.mandates.domain import ShiftDoc  # noqa: E402

from tests.cloud.test_belt_mandates import USER, WS, _seed_run, store  # noqa: E402,F401

pytestmark = pytest.mark.usefixtures("any_repo_root", "store")


async def _mandate(cadence: str = "continuous", budget: dict | None = None) -> str:
    created = await mandate_service.create_mandate(
        WS,
        USER,
        {
            "name": f"m-{cadence}",
            "surface": {"repo_id": "/tmp/x"},
            "charter": {"goal": "keep it healthy", "cadence": cadence, "budget": budget or {}},
        },
    )
    return created["mandate"]["id"]


async def _age_runs(store, hours: float) -> None:  # noqa: F811
    """Backdate every run's last store write: an orphan nothing touches."""
    import aiosqlite

    async with aiosqlite.connect(store._db_path) as db:
        await db.execute(
            "UPDATE instinct_actions SET updated_at = datetime('now', ?)", (f"-{hours} hours",)
        )
        await db.commit()


async def _sighting(mandate_id: str, text: str = "buy button does nothing") -> None:
    await mandate_service.file_feedback(
        WS, USER, mandate_id, {"text": text, "severity": 3, "source": "support"}
    )


async def _shift(mandate_id: str, state: str, age: timedelta, plan_action_id=None) -> None:
    shift = ShiftDoc(
        workspace=WS, mandate_id=mandate_id, no=1, state=state, plan_action_id=plan_action_id
    )
    await shift.insert()
    # Backdate AFTER insert: the insert hook stamps createdAt with the wall clock.
    await shift.set({ShiftDoc.createdAt: datetime.now(UTC) - age})


async def _due(mandate_id: str) -> bool:
    rows = await mandate_service.list_cadence_due(datetime.now(UTC))
    return mandate_id in {r["mandate_id"] for r in rows}


async def test_never_shifted_is_due_only_with_an_open_sighting(mongo_db):
    mid = await _mandate()
    assert not await _due(mid), "nothing to work on"
    await _sighting(mid)
    assert await _due(mid)


async def test_waits_out_the_minimum_gap(mongo_db, monkeypatch):
    mid = await _mandate()
    await _sighting(mid)
    await _shift(mid, "done", timedelta(minutes=2))
    assert not await _due(mid), "inside the 5 minute gap"
    monkeypatch.setenv("POCKETPAW_MANDATE_CONTINUOUS_MIN_GAP_MINUTES", "1")
    assert await _due(mid)


async def test_done_shift_past_the_gap_is_due(mongo_db):
    mid = await _mandate()
    await _sighting(mid)
    await _shift(mid, "done", timedelta(minutes=10))
    assert await _due(mid)


async def test_a_plan_waiting_for_a_human_is_never_released(mongo_db):
    mid = await _mandate()
    await _sighting(mid)
    await _shift(mid, "in_gate", timedelta(hours=10))
    assert not await _due(mid)


async def test_a_full_line_waits_until_an_idle_orphan_stops_counting(mongo_db, store):  # noqa: F811
    mid = await _mandate(budget={"max_in_flight": 1})
    await _sighting(mid)
    plan_id = "plan-1"
    await _seed_run(
        store, mid, "fix the buy button", plan_action_id=plan_id, task_index=1, station_pending=True
    )
    await _shift(mid, "done", timedelta(minutes=30), plan_action_id=plan_id)
    assert not await _due(mid), "the line's one slot is taken by a queued run"

    await _age_runs(store, 4)
    assert await _due(mid), "no store write for the 3 h stall timeout: an orphan"


async def test_stall_timeout_is_env_overridable(mongo_db, store, monkeypatch):  # noqa: F811
    mid = await _mandate(budget={"max_in_flight": 1})
    await _sighting(mid)
    await _seed_run(store, mid, "t", plan_action_id="p", task_index=1, station_pending=True)
    await _shift(mid, "done", timedelta(minutes=30), plan_action_id="p")
    await _age_runs(store, 1.5)
    assert not await _due(mid)
    monkeypatch.setenv("POCKETPAW_MANDATE_CONTINUOUS_STALL_HOURS", "1")
    assert await _due(mid)


async def test_a_stuck_planning_shift_is_released_by_the_stall_timeout(mongo_db):
    mid = await _mandate()
    await _sighting(mid)
    await _shift(mid, "planning", timedelta(hours=1))
    assert not await _due(mid)
    await ShiftDoc.find(ShiftDoc.mandate_id == mid).delete()
    await _shift(mid, "planning", timedelta(hours=4))
    assert await _due(mid)


async def test_a_stood_down_shift_waits_for_a_new_sighting(mongo_db):
    """The foreman already saw this backlog and planned nothing; firing again
    on the same backlog would be one wasted LLM call per gap."""
    from pocketpaw_ee.cloud.mandates.domain import SightingDoc

    mid = await _mandate()
    await _sighting(mid, "old signal")
    old = await SightingDoc.find_one(SightingDoc.mandate_id == mid)
    await old.set({SightingDoc.ts: datetime.now(UTC) - timedelta(hours=1)})
    await _shift(mid, "stood_down", timedelta(minutes=30))
    assert not await _due(mid)
    await _sighting(mid, "new signal")
    assert await _due(mid)


async def test_a_stood_down_shift_wakes_when_a_run_settles_after_it(mongo_db, store):  # noqa: F811
    """The foreman stood down waiting on a run in flight. That run was later
    released (rejected); its sighting is open again with nothing new filed, so
    the line must wake instead of idling until the next sighting."""
    import aiosqlite
    from pocketpaw_ee.cloud.mandates.domain import SightingDoc

    mid = await _mandate()
    await _sighting(mid, "issue #10")
    old = await SightingDoc.find_one(SightingDoc.mandate_id == mid)
    await old.set({SightingDoc.ts: datetime.now(UTC) - timedelta(hours=1)})
    run = await _seed_run(store, mid, "issue #10", station_pending=True)
    await _shift(mid, "stood_down", timedelta(minutes=30))
    assert not await _due(mid), "the run is still in flight"

    await store.reject(run.id, reason="silent worker", rejector="system:belt-cloud")
    assert await _due(mid), "the run it waited on settled after the stand-down"

    async with aiosqlite.connect(store._db_path) as db:
        await db.execute("UPDATE instinct_actions SET updated_at = datetime('now', '-1 hour')")
        await db.commit()
    assert not await _due(mid), "a run that settled before the stand-down was already seen"


async def test_gate_teaching_alone_is_not_work(mongo_db):
    mid = await _mandate()
    await mandate_service.file_feedback(
        WS, USER, mid, {"kind": "reject", "reason": "too risky", "shift_no": 1, "task_title": "x"}
    )
    await _shift(mid, "done", timedelta(minutes=30))
    assert not await _due(mid)


async def test_a_paused_continuous_mandate_is_not_due(mongo_db):
    from pocketpaw_ee.cloud.mandates.domain import MandateDoc

    mid = await _mandate()
    await _sighting(mid)
    doc = await MandateDoc.find_one(MandateDoc.workspace == WS)
    await doc.set({MandateDoc.status: "paused"})
    assert not await _due(mid)


async def test_daily_and_weekly_are_unchanged(mongo_db):
    weekly = await _mandate("weekly")
    await _shift(weekly, "done", timedelta(minutes=30))
    assert not await _due(weekly)


async def test_loop_ticks_at_the_continuous_interval_while_one_exists(mongo_db, monkeypatch):
    assert await scheduler_mod._next_sleep(3600) == 3600
    await _mandate("weekly")
    assert await scheduler_mod._next_sleep(3600) == 3600
    await _mandate("continuous")
    assert await scheduler_mod._next_sleep(3600) == 60
    monkeypatch.setenv("POCKETPAW_MANDATE_SCHEDULER_CONTINUOUS_INTERVAL", "30")
    assert await scheduler_mod._next_sleep(3600) == 30
    assert await scheduler_mod._next_sleep(10) == 10, "never slower than asked"


async def test_due_continuous_mandate_fires_through_the_existing_trigger(mongo_db):
    mid = await _mandate()
    await _sighting(mid)
    fired: list = []

    async def trigger(workspace_id, user_id, mandate_id):
        fired.append((workspace_id, user_id, mandate_id))
        return {}

    assert await scheduler_mod.run_scheduler_tick(trigger=trigger) == [mid]
    assert fired == [(WS, "system:scheduler", mid)]
