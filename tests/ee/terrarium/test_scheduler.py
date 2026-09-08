# tests/ee/terrarium/test_scheduler.py — the clock. Pure cadence arithmetic
# (running vs dormant, due vs not), the DB sweep flipping status, a failing
# universe not blocking the next, event reads stamping last_viewed_at, and the
# loop never starting without the gate env.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pocketpaw_ee")
pytest.importorskip("mongomock_motor")

from pocketpaw_ee.terrarium import scheduler, service  # noqa: E402
from pocketpaw_ee.terrarium.domain import UniverseDoc  # noqa: E402

from .conftest import create_universe  # noqa: E402

T0 = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
TIME = {"world_day_seconds": 3600, "ticks_per_day": 12, "dormant_ticks_per_day": 1}


@pytest.mark.parametrize(
    ("ticked_ago", "viewed_ago", "due", "status"),
    [
        (None, 0, True, "running"),  # never ticked: due now
        (299, 0, False, "running"),  # 3600/12 = 300s per tick
        (300, 0, True, "running"),
        (300, 3601, False, "dormant"),  # unwatched: 1 tick per 3600s
        (3600, 3601, True, "dormant"),
        (3600, None, True, "dormant"),  # never viewed counts as dormant
    ],
)
def test_due_state_arithmetic(ticked_ago, viewed_ago, due, status):
    got = scheduler.due_state(
        TIME,
        now=T0,
        last_tick_at=None if ticked_ago is None else T0 - timedelta(seconds=ticked_ago),
        last_viewed_at=None if viewed_ago is None else T0 - timedelta(seconds=viewed_ago),
    )
    assert got == (due, status)


def test_due_state_accepts_naive_datetimes():
    naive = T0.replace(tzinfo=None)
    assert scheduler.due_state(TIME, now=T0, last_tick_at=naive, last_viewed_at=naive) == (
        False,
        "running",
    )


async def test_sweep_ticks_due_universes_and_flips_dormant(client):
    a = create_universe(client, founders=1)
    b = create_universe(client, founders=1)
    doc_a = await UniverseDoc.get(a["id"])
    doc_a.last_viewed_at = T0  # watched right now (createdAt is the real clock)
    await doc_a.save()
    doc_b = await UniverseDoc.get(b["id"])
    doc_b.last_viewed_at = T0 - timedelta(days=2)
    doc_b.last_tick_at = T0 - timedelta(seconds=600)  # due if running, not if dormant
    await doc_b.save()

    fired: list[tuple] = []

    async def fake_tick(ws, user, uid, n):
        fired.append((ws, uid, n))
        return {}

    ticked = await scheduler.run_scheduler_tick(now=lambda: T0, trigger=fake_tick)
    assert ticked == [a["id"]]
    assert fired == [("ws-terra", a["id"], 1)]
    assert (await UniverseDoc.get(b["id"])).status == "dormant"
    assert (await UniverseDoc.get(a["id"])).status == "running"


async def test_a_failing_universe_does_not_block_the_next(client):
    a = create_universe(client, founders=1)
    b = create_universe(client, founders=1)

    async def flaky(ws, user, uid, n):
        if uid == a["id"]:
            raise RuntimeError("boom")
        return {}

    ticked = await scheduler.run_scheduler_tick(now=lambda: T0, trigger=flaky)
    assert ticked == [b["id"]]


async def test_real_tick_stamps_last_tick_at_and_advances(client):
    uni = create_universe(client, founders=1)
    ticked = await scheduler.run_scheduler_tick(now=lambda: T0)
    assert ticked == [uni["id"]]
    doc = await UniverseDoc.get(uni["id"])
    assert doc.tick == 1 and doc.last_tick_at is not None
    # Just ticked -> not due again at the same instant.
    assert await scheduler.run_scheduler_tick(now=lambda: datetime.now(UTC)) == []


async def test_event_reads_stamp_last_viewed_at(client, monkeypatch):
    uni = create_universe(client, public=True, founders=1)
    doc = await UniverseDoc.get(uni["id"])
    doc.last_viewed_at = None
    await doc.save()
    client.get(f"/terrarium/universes/{uni['id']}/events")
    assert (await UniverseDoc.get(uni["id"])).last_viewed_at is not None

    doc = await UniverseDoc.get(uni["id"])
    doc.last_viewed_at = None
    await doc.save()
    monkeypatch.setenv("TERRARIUM_PUBLIC_ENABLED", "1")
    client.get(f"/terrarium/public/universes/{uni['id']}/events")
    assert (await UniverseDoc.get(uni["id"])).last_viewed_at is not None


async def test_the_clock_is_not_on_the_wire(client):
    uni = create_universe(client, founders=1)
    assert "last_tick_at" not in uni and "last_viewed_at" not in uni


async def test_sweeper_never_starts_without_the_gate(monkeypatch):
    monkeypatch.delenv("POCKETPAW_CLOUD_SCHEDULER_ENABLED", raising=False)
    assert await scheduler.reconcile_scheduler() == 0
    assert not scheduler.is_running()
    monkeypatch.setenv("POCKETPAW_CLOUD_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("POCKETPAW_TERRARIUM_SCHEDULER_INTERVAL", "3600")
    try:
        assert await scheduler.reconcile_scheduler() == 1
        assert scheduler.is_running()
        assert await scheduler.reconcile_scheduler() == 0  # idempotent
    finally:
        await scheduler.shutdown_scheduler()
    assert not scheduler.is_running()


async def test_scheduler_sweep_skips_archived(client):
    uni = create_universe(client, founders=1)
    doc = await UniverseDoc.get(uni["id"])
    doc.status = "archived"
    await doc.save()
    assert await service.scheduler_sweep(T0) == []


# --- the wiring, not just the logic --------------------------------------
#
# The clock was registered with `@app.on_event("startup")`, which FastAPI
# silently drops when the app is built with a custom lifespan= (this host's
# default). Every unit test passed and no universe ever ticked. pocketpaw#2097
# then fixed that generally: mount_cloud collects hooks through its own
# `on_startup`/`on_shutdown` decorators and the composed lifespan drains them.
#
# So the gate is not "does the loop work" but "is the clock registered through
# the mechanism that is actually drained". Registering it the old way again
# would look fine and start nothing — which is exactly what a rebase onto that
# fix nearly shipped.


def test_the_clock_registers_through_the_drained_hook_list() -> None:
    """mount_cloud must wire the clock with `on_startup`, not `@app.on_event`.

    Mutation that must fail this: change the terrarium block back to
    `@app.on_event("startup")`.
    """
    import inspect

    from pocketpaw_ee import cloud

    src = inspect.getsource(cloud.mount_cloud)
    start = src.index("_start_terrarium_clock")
    block = src[max(0, start - 400) : start + 200]

    assert "@on_startup" in block, (
        "the terrarium clock is not on the drained hook list — "
        "@app.on_event handlers are collected and never run under a custom lifespan"
    )
    assert "@app.on_event" not in block
    assert "reconcile_scheduler" in block


def test_the_clock_is_stopped_through_the_same_mechanism() -> None:
    import inspect

    from pocketpaw_ee import cloud

    src = inspect.getsource(cloud.mount_cloud)
    stop = src.index("_stop_terrarium_clock")
    block = src[max(0, stop - 200) : stop + 200]

    assert "@on_shutdown" in block
    assert "shutdown_scheduler" in block
