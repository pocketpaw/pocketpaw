"""Burnup-chart shape contract test.

Locks down what the frontend's BurndownChart component consumes from
``CycleResponse.daily``:

- four parallel series (scope, started, completed, plus an is_weekend
  flag per point)
- ``scope`` is the total visible at snapshot time (typically flat unless
  new tasks are added mid-cycle)
- ``started`` is monotone non-decreasing across days
- ``completed`` is monotone non-decreasing across days and never
  exceeds ``started``
- ``is_weekend`` is set correctly for Saturday/Sunday so the chart can
  flatten the dashed ideal target on those days
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.cycles import service as cycles_service
from pocketpaw_ee.cloud.cycles.dto import CreateCycleRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx() -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id="w1",
        request_id="burnup",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


class _FakeTask:
    def __init__(self, status: str, task_id: str = "t1") -> None:
        self.status = status
        self.id = task_id


def _install_dynamic_tasks(get_tasks_callable) -> None:
    """Install a stub Tasks package whose ``agent_list_tasks`` reads the
    latest result from ``get_tasks_callable``. Lets the test simulate a
    cycle's task progression day by day without rebuilding the module.
    """
    mod_tasks = types.ModuleType("pocketpaw_ee.cloud.tasks")
    mod_service = types.ModuleType("pocketpaw_ee.cloud.tasks.service")
    mod_dto = types.ModuleType("pocketpaw_ee.cloud.tasks.dto")

    class _ListReq:
        def __init__(self, cycle_id: str | None = None, **_: object) -> None:
            self.cycle_id = cycle_id

    mod_dto.ListTasksRequest = _ListReq  # type: ignore[attr-defined]

    async def _list(_ctx, _body):  # type: ignore[no-untyped-def]
        return list(get_tasks_callable())

    mod_service.agent_list_tasks = _list  # type: ignore[attr-defined]
    mod_tasks.service = mod_service  # type: ignore[attr-defined]
    mod_tasks.dto = mod_dto  # type: ignore[attr-defined]

    sys.modules["pocketpaw_ee.cloud.tasks"] = mod_tasks
    sys.modules["pocketpaw_ee.cloud.tasks.service"] = mod_service
    sys.modules["pocketpaw_ee.cloud.tasks.dto"] = mod_dto


def _uninstall() -> None:
    for name in (
        "pocketpaw_ee.cloud.tasks",
        "pocketpaw_ee.cloud.tasks.service",
        "pocketpaw_ee.cloud.tasks.dto",
    ):
        sys.modules.pop(name, None)


@pytest.fixture
def task_progression():
    """Yield a list slot the test can mutate to advance the task states
    across simulated snapshot days."""
    holder: dict[str, list[_FakeTask]] = {"tasks": []}
    _install_dynamic_tasks(lambda: holder["tasks"])
    try:
        yield holder
    finally:
        _uninstall()


async def test_daily_series_shape_matches_frontend_expectation(task_progression) -> None:
    """Walk a cycle through five days; verify the resulting series
    matches the burnup chart contract."""
    # Day 0: cycle created with 5 tasks all in "proposed".
    task_progression["tasks"] = [_FakeTask("proposed", f"t{i}") for i in range(5)]

    cycle = await cycles_service.agent_create_cycle(
        _ctx(),
        CreateCycleRequest(
            name="May Wedding",
            pocket_id="p1",
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            status="active",
        ),
    )

    # Simulate 7 days starting Monday 2026-05-11: started/completed counts
    # climb over time, scope stays at 5.
    plan = [
        (date(2026, 5, 11), ["proposed", "proposed", "in_progress", "proposed", "proposed"]),
        (date(2026, 5, 12), ["in_progress", "proposed", "in_progress", "proposed", "proposed"]),
        (date(2026, 5, 13), ["in_progress", "in_progress", "done", "proposed", "proposed"]),
        (date(2026, 5, 14), ["in_progress", "in_progress", "done", "in_progress", "proposed"]),
        (date(2026, 5, 15), ["done", "in_progress", "done", "in_progress", "proposed"]),
        (date(2026, 5, 16), ["done", "in_progress", "done", "in_progress", "proposed"]),  # Sat
        (date(2026, 5, 17), ["done", "in_progress", "done", "in_progress", "proposed"]),  # Sun
    ]
    for snap_date, statuses in plan:
        task_progression["tasks"] = [_FakeTask(s, f"t{i}") for i, s in enumerate(statuses)]
        await cycles_service._snapshot_cycle_daily(_ctx(), cycle.id, today=snap_date)

    fetched = await cycles_service.agent_get_cycle(_ctx(), cycle.id)
    series = fetched.daily
    assert len(series) == 7
    # Dates are stored in append order.
    assert [p.date for p in series] == [d.isoformat() for d, _ in plan]

    # Scope is flat — 5 tasks throughout.
    assert all(p.scope == 5 for p in series)

    # Started is non-decreasing.
    started = [p.started for p in series]
    assert all(b >= a for a, b in zip(started, started[1:])), started

    # Completed is non-decreasing.
    completed = [p.completed for p in series]
    assert all(b >= a for a, b in zip(completed, completed[1:])), completed

    # Completed never exceeds started — the frontend's chart depends on this
    # so the "in flight" wedge between the two lines is always non-negative.
    assert all(c <= s for c, s in zip(completed, started))

    # Final-day numbers match the plan: 2 done, 4 started.
    assert series[-1].completed == 2
    assert series[-1].started == 4
    assert series[-1].scope == 5

    # is_weekend correctly tagged. Saturday + Sunday in the plan.
    weekend_flags = {p.date: p.is_weekend for p in series}
    assert weekend_flags["2026-05-11"] is False  # Monday
    assert weekend_flags["2026-05-15"] is False  # Friday
    assert weekend_flags["2026-05-16"] is True  # Saturday
    assert weekend_flags["2026-05-17"] is True  # Sunday


async def test_daily_series_four_visible_fields(task_progression) -> None:
    """The frontend reads exactly four fields per point — verify the
    DTO carries them with stable names."""
    task_progression["tasks"] = [_FakeTask("in_progress", "t0")]

    cycle = await cycles_service.agent_create_cycle(
        _ctx(),
        CreateCycleRequest(
            name="x", start=date(2026, 5, 1), end=date(2026, 5, 31), status="active"
        ),
    )
    await cycles_service._snapshot_cycle_daily(_ctx(), cycle.id, today=date(2026, 5, 11))

    fetched = await cycles_service.agent_get_cycle(_ctx(), cycle.id)
    point = fetched.daily[0].model_dump()
    assert set(point.keys()) == {"date", "scope", "started", "completed", "is_weekend"}
