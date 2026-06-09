# tests/cloud/member_ingest/test_member_ingest_sweep.py
# Created: 2026-06-08 — VIP Onboarding Phase B (per-user ingest worker).
#
# Pins the sweep + scheduler contract:
#
#   1. list_connected_members enumerates ONLY scope=user, enabled, gmail/
#      gcalendar connector rows — and never a disabled or workspace-scoped
#      one.
#   2. run_ingest_sweep ingests every connected member into THEIR OWN scope
#      (cross-member isolation holds across the whole sweep), under a bounded
#      concurrency cap (never more than the limit run at once).
#   3. one member's ingest failing does not abort the sweep for the others.
#   4. the scheduler start()/stop() lifecycle spawns and cancels cleanly, and
#      tick() runs a single sweep without a background task.

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.member_ingest import scheduler as sched_mod  # noqa: E402
from pocketpaw_ee.cloud.member_ingest import service as ingest_service  # noqa: E402
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector  # noqa: E402

pytestmark = pytest.mark.asyncio


async def _seed_connector(workspace, name, *, scope, user_id=None, enabled=True):
    doc = WorkspaceConnector(
        workspace=workspace,
        name=name,
        enabled=enabled,
        scope=scope,
        user_id=user_id,
    )
    await doc.insert()
    return doc


async def test_list_connected_members_only_user_scoped_enabled(mongo_db):  # noqa: ARG001
    # Two members with per-user Gmail, one with gcalendar, plus noise rows
    # that must be excluded.
    await _seed_connector("w1", "gmail", scope="user", user_id="alice")
    await _seed_connector("w1", "gcalendar", scope="user", user_id="alice")
    await _seed_connector("w1", "gmail", scope="user", user_id="bob")
    # Noise: workspace-scoped gmail (not per-user) — excluded.
    await _seed_connector("w1", "gmail", scope="workspace", user_id=None)
    # Noise: disabled per-user gmail — excluded.
    await _seed_connector("w1", "gmail", scope="user", user_id="carol", enabled=False)
    # Noise: an unrelated connector — excluded.
    await _seed_connector("w1", "stripe", scope="user", user_id="alice")
    # Noise: another workspace — excluded when we filter by w1.
    await _seed_connector("w2", "gmail", scope="user", user_id="dave")

    members = await ingest_service.list_connected_members(workspace_id="w1")

    # Grouped by (workspace, member) — alice appears once even with 2 sources.
    pairs = {(m["workspace_id"], m["member_id"]) for m in members}
    assert pairs == {("w1", "alice"), ("w1", "bob")}
    assert "carol" not in {m["member_id"] for m in members}
    assert "dave" not in {m["member_id"] for m in members}


async def test_sweep_isolates_members_and_bounds_concurrency(mongo_db):  # noqa: ARG001
    await _seed_connector("w1", "gmail", scope="user", user_id="alice")
    await _seed_connector("w1", "gmail", scope="user", user_id="bob")
    await _seed_connector("w1", "gmail", scope="user", user_id="carol")
    await _seed_connector("w1", "gcalendar", scope="user", user_id="dave")

    written_scopes: list[str] = []
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def fake_ingest_member(workspace_id, member_id, **_kw):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.01)  # hold the slot so concurrency is observable
            written_scopes.append(f"user:{member_id}")
            return {"status": "ok", "member_id": member_id}
        finally:
            async with lock:
                in_flight -= 1

    summary = await ingest_service.run_ingest_sweep(
        workspace_id="w1",
        concurrency=2,
        ingest_fn=fake_ingest_member,
    )

    # Every connected member was swept into their OWN scope, all distinct.
    assert set(written_scopes) == {
        "user:alice",
        "user:bob",
        "user:carol",
        "user:dave",
    }
    assert len(written_scopes) == len(set(written_scopes))  # no double-write
    # Concurrency was actually bounded by the cap.
    assert max_in_flight <= 2
    assert summary["members"] == 4
    assert summary["ok"] == 4


async def test_sweep_one_member_failure_does_not_abort_others(mongo_db):  # noqa: ARG001
    await _seed_connector("w1", "gmail", scope="user", user_id="alice")
    await _seed_connector("w1", "gmail", scope="user", user_id="bob")
    await _seed_connector("w1", "gmail", scope="user", user_id="carol")

    done: list[str] = []

    async def flaky_ingest(workspace_id, member_id, **_kw):
        if member_id == "bob":
            raise RuntimeError("bob's token is toast")
        done.append(member_id)
        return {"status": "ok", "member_id": member_id}

    summary = await ingest_service.run_ingest_sweep(
        workspace_id="w1",
        concurrency=3,
        ingest_fn=flaky_ingest,
    )

    # Alice + Carol completed despite Bob blowing up.
    assert set(done) == {"alice", "carol"}
    assert summary["members"] == 3
    assert summary["ok"] == 2
    assert summary["errors"] == 1


async def test_scheduler_tick_runs_one_sweep(mongo_db, monkeypatch):  # noqa: ARG001
    calls: list[int] = []

    async def fake_sweep(**_kw):
        calls.append(1)
        return {"members": 0, "ok": 0, "errors": 0}

    monkeypatch.setattr(ingest_service, "run_ingest_sweep", fake_sweep)
    sched_mod.reset_scheduler_for_tests()

    scheduler = sched_mod.MemberIngestScheduler(interval_seconds=1)
    swept = await scheduler.tick()

    assert calls == [1]
    assert swept["members"] == 0


async def test_scheduler_start_stop_lifecycle(mongo_db, monkeypatch):  # noqa: ARG001
    async def fake_sweep(**_kw):
        return {"members": 0, "ok": 0, "errors": 0}

    monkeypatch.setattr(ingest_service, "run_ingest_sweep", fake_sweep)
    sched_mod.reset_scheduler_for_tests()

    scheduler = sched_mod.MemberIngestScheduler(interval_seconds=60)
    await scheduler.start()
    # Idempotent — second start is a no-op, not a second task.
    await scheduler.start()
    assert scheduler._task is not None  # noqa: SLF001
    assert not scheduler._task.done()  # noqa: SLF001

    await scheduler.stop()
    assert scheduler._task is None  # noqa: SLF001
    # Safe to stop twice.
    await scheduler.stop()
