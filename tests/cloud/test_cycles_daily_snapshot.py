"""Daily-snapshot job tests.

Covers the idempotency rule, the weekend flag, and the no-op behavior
when Tasks (PR 2) isn't available on this branch. The full
scope/started/completed projection is exercised in
``test_cycles_burnup_shape.py``.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.realtime.events import CycleSnapshotted
from ee.cloud.cycles import service as cycles_service
from ee.cloud.cycles.dto import CreateCycleRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx() -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id="w1",
        request_id="snap",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


class _FakeTask:
    """Lightweight stand-in for a Tasks domain object.

    Tasks (PR 2) ships objects with ``status`` and ``id`` attributes; this
    fake matches that shape so the cycles service's projection helpers
    don't have to know whether the real entity is loaded.
    """

    def __init__(self, status: str, task_id: str = "t1") -> None:
        self.status = status
        self.id = task_id


def _install_fake_tasks(tasks_to_return: list[_FakeTask] | None = None) -> None:
    """Install a stub ``ee.cloud.tasks`` package so the cycles service's
    lazy import picks up controllable test doubles.

    Tests that want to assert against real PR-2 tasks would import the
    real module; tests in this file deliberately stub it so the snapshot
    job's behavior can be exercised without depending on PR 2 having
    landed.
    """
    mod_tasks = types.ModuleType("ee.cloud.tasks")
    mod_service = types.ModuleType("ee.cloud.tasks.service")
    mod_dto = types.ModuleType("ee.cloud.tasks.dto")

    class _ListReq:
        def __init__(self, cycle_id: str | None = None, **_: object) -> None:
            self.cycle_id = cycle_id

    mod_dto.ListTasksRequest = _ListReq  # type: ignore[attr-defined]

    async def _list(_ctx, _body):  # type: ignore[no-untyped-def]
        return list(tasks_to_return or [])

    mod_service.agent_list_tasks = _list  # type: ignore[attr-defined]
    mod_tasks.service = mod_service  # type: ignore[attr-defined]
    mod_tasks.dto = mod_dto  # type: ignore[attr-defined]

    sys.modules["ee.cloud.tasks"] = mod_tasks
    sys.modules["ee.cloud.tasks.service"] = mod_service
    sys.modules["ee.cloud.tasks.dto"] = mod_dto


def _uninstall_fake_tasks() -> None:
    for name in ("ee.cloud.tasks", "ee.cloud.tasks.service", "ee.cloud.tasks.dto"):
        sys.modules.pop(name, None)


@pytest.fixture
def fake_tasks():
    """Yield a list the caller can mutate to control the snapshot result."""
    bucket: list[_FakeTask] = []
    # Bind by reference: the stub closes over ``bucket``, so appends to it
    # after install still affect subsequent calls.
    _install_fake_tasks(bucket)
    try:
        yield bucket
    finally:
        _uninstall_fake_tasks()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_snapshot_appends_a_point(fake_tasks, recording_bus) -> None:
    fake_tasks.extend([_FakeTask("proposed"), _FakeTask("in_progress"), _FakeTask("done")])

    cycle = await cycles_service.agent_create_cycle(
        _ctx(),
        CreateCycleRequest(
            name="x",
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            status="active",
        ),
    )

    today = date(2026, 5, 13)  # A Wednesday
    point = await cycles_service._snapshot_cycle_daily(_ctx(), cycle.id, today=today)
    assert point is not None
    assert point.date == "2026-05-13"
    assert point.scope == 3
    assert point.started == 2  # in_progress + done
    assert point.completed == 1
    assert point.is_weekend is False

    refreshed = await cycles_service.agent_get_cycle(_ctx(), cycle.id)
    assert len(refreshed.daily) == 1
    assert refreshed.daily[0].date == "2026-05-13"

    snap_events = [e for e in recording_bus.events if isinstance(e, CycleSnapshotted)]
    assert len(snap_events) == 1
    assert snap_events[0].data["cycle_id"] == cycle.id
    assert snap_events[0].data["daily_point"]["date"] == "2026-05-13"


async def test_snapshot_is_idempotent_within_day(fake_tasks) -> None:
    fake_tasks.append(_FakeTask("proposed"))
    cycle = await cycles_service.agent_create_cycle(
        _ctx(),
        CreateCycleRequest(
            name="x", start=date(2026, 5, 1), end=date(2026, 5, 31), status="active"
        ),
    )
    today = date(2026, 5, 13)
    p1 = await cycles_service._snapshot_cycle_daily(_ctx(), cycle.id, today=today)
    p2 = await cycles_service._snapshot_cycle_daily(_ctx(), cycle.id, today=today)
    assert p1 is not None
    assert p2 is None  # Second call is a no-op

    refreshed = await cycles_service.agent_get_cycle(_ctx(), cycle.id)
    assert len(refreshed.daily) == 1


async def test_snapshot_weekend_flag(fake_tasks) -> None:
    fake_tasks.append(_FakeTask("proposed"))
    cycle = await cycles_service.agent_create_cycle(
        _ctx(),
        CreateCycleRequest(
            name="x", start=date(2026, 5, 1), end=date(2026, 5, 31), status="active"
        ),
    )
    saturday = date(2026, 5, 16)  # weekday() == 5
    point = await cycles_service._snapshot_cycle_daily(_ctx(), cycle.id, today=saturday)
    assert point is not None
    assert point.is_weekend is True

    sunday = date(2026, 5, 17)
    point2 = await cycles_service._snapshot_cycle_daily(_ctx(), cycle.id, today=sunday)
    assert point2 is not None
    assert point2.is_weekend is True


async def test_snapshot_skips_completed_cycle(fake_tasks) -> None:
    fake_tasks.append(_FakeTask("proposed"))
    cycle = await cycles_service.agent_create_cycle(
        _ctx(),
        CreateCycleRequest(
            name="x", start=date(2026, 5, 1), end=date(2026, 5, 31), status="active"
        ),
    )
    await cycles_service.agent_close_cycle(_ctx(), cycle.id)
    point = await cycles_service._snapshot_cycle_daily(_ctx(), cycle.id)
    assert point is None


# ---------------------------------------------------------------------------
# Graceful degrade when Tasks unavailable
# ---------------------------------------------------------------------------


async def test_snapshot_noop_when_tasks_unavailable(monkeypatch) -> None:
    """When the Tasks entity isn't on this branch, the snapshot job logs
    and returns None rather than crashing."""
    cycle = await cycles_service.agent_create_cycle(
        _ctx(),
        CreateCycleRequest(
            name="x", start=date(2026, 5, 1), end=date(2026, 5, 31), status="active"
        ),
    )
    # Defensive cleanup: make sure no other test left a stub installed.
    _uninstall_fake_tasks()

    async def _none(*_args, **_kwargs):
        return None

    # Monkey-patch the helper that lazy-imports tasks so the snapshot path
    # behaves as if PR 2 hadn't merged, even if other suites have left a
    # stub on ``sys.modules``.
    monkeypatch.setattr(cycles_service, "_tasks_for_cycle", _none)
    point = await cycles_service._snapshot_cycle_daily(_ctx(), cycle.id)
    assert point is None


# ---------------------------------------------------------------------------
# snapshot_job wrapper
# ---------------------------------------------------------------------------


async def test_snapshot_all_active_handles_multiple_workspaces(fake_tasks) -> None:
    """The job-level wrapper iterates every active cycle in the
    workspace and counts how many got a fresh point."""
    from ee.cloud.cycles import snapshot_job

    fake_tasks.append(_FakeTask("in_progress"))

    ctx = _ctx()
    await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="active-1",
            pocket_id="p1",
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            status="active",
        ),
    )
    await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="active-2",
            pocket_id="p2",
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            status="active",
        ),
    )
    await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="upcoming",
            pocket_id="p3",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            status="upcoming",
        ),
    )

    count = await snapshot_job.snapshot_all_active("w1")
    # Two active cycles, both should be snapshotted once.
    assert count == 2

    # Idempotent on second pass
    count2 = await snapshot_job.snapshot_all_active("w1")
    assert count2 == 0
