"""Tests for ``ee.cloud.cycles.service`` — CRUD + status transitions.

Exercise the service-level API directly against the shared mongomock-motor
fixture. The ``recording_bus`` autouse fixture captures emitted events.

Tasks-composition assertions (status counters, rollover-on-close) run
against the live Tasks service now that PR 2 has merged. The
``_tasks_available`` probe is kept as a safety net for trunk forks that
predate PR 2 — those still see the gated paths skip rather than crash.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.errors import ConflictError, Forbidden, NotFound
from ee.cloud._core.realtime.events import (
    CycleClosed,
    CycleCreated,
    CycleUpdated,
)
from ee.cloud.cycles import service as cycles_service
from ee.cloud.cycles.dto import CreateCycleRequest, UpdateCycleRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace: str = "w1", user: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _tasks_available() -> bool:
    """Returns True only when PR 2's Tasks entity is mergeable on this
    branch — used to gate task-composition assertions."""
    try:
        from ee.cloud.tasks import service as _  # noqa: F401
        from ee.cloud.tasks.dto import ListTasksRequest as _LTR  # noqa: F401
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_persists_with_workspace_and_creator(recording_bus) -> None:
    ctx = _ctx(workspace="w1", user="shawn")
    body = CreateCycleRequest(
        name="Crestline · May 23 Wedding",
        description="4-week prep window",
        pocket_id="p1",
        start=date(2026, 5, 1),
        end=date(2026, 5, 29),
        status="upcoming",
    )
    out = await cycles_service.agent_create_cycle(ctx, body)
    assert out.name == "Crestline · May 23 Wedding"
    assert out.workspace_id == "w1"
    assert out.created_by == "shawn"
    assert out.status == "upcoming"
    assert out.start == "2026-05-01"
    assert out.end == "2026-05-29"

    created_events = [e for e in recording_bus.events if isinstance(e, CycleCreated)]
    assert len(created_events) == 1
    assert created_events[0].data["id"] == out.id


async def test_create_rejects_start_after_end() -> None:
    with pytest.raises(ValueError):
        CreateCycleRequest(
            name="bad",
            start=date(2026, 6, 1),
            end=date(2026, 5, 1),
        )


async def test_create_rejects_overlap_on_same_pocket() -> None:
    ctx = _ctx()
    await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="May Wedding",
            pocket_id="p1",
            start=date(2026, 5, 1),
            end=date(2026, 5, 29),
            status="active",
        ),
    )
    with pytest.raises(ConflictError):
        await cycles_service.agent_create_cycle(
            ctx,
            CreateCycleRequest(
                name="May Wedding Take Two",
                pocket_id="p1",
                start=date(2026, 5, 15),
                end=date(2026, 6, 10),
                status="active",
            ),
        )


async def test_create_allows_overlap_on_different_pockets() -> None:
    """Workspaces routinely run multiple engagements simultaneously on
    different pockets — the overlap rule applies per-pocket only."""
    ctx = _ctx()
    await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="May Wedding",
            pocket_id="p1",
            start=date(2026, 5, 1),
            end=date(2026, 5, 29),
            status="active",
        ),
    )
    out = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="May Summit",
            pocket_id="p2",
            start=date(2026, 5, 15),
            end=date(2026, 6, 10),
            status="active",
        ),
    )
    assert out.pocket_id == "p2"


async def test_create_allows_workspace_wide_overlap() -> None:
    """Workspace-wide cycles (no ``pocket_id``) are allowed to coexist on
    overlapping dates. Operators routinely run multiple workspace-level
    cycles in parallel — different events, workstreams, experiments —
    all at the workspace tier. Locks in the 2026-05-19 relaxation in
    ``agent_create_cycle`` so a future refactor can't silently
    re-collapse the overlap check to ``pocket_id=None``.
    """
    ctx = _ctx()
    first = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="Workspace Cycle A",
            pocket_id=None,
            start=date(2026, 5, 1),
            end=date(2026, 5, 29),
            status="active",
        ),
    )
    second = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="Workspace Cycle B",
            pocket_id=None,
            start=date(2026, 5, 15),
            end=date(2026, 6, 10),
            status="active",
        ),
    )
    assert first.pocket_id is None
    assert second.pocket_id is None
    assert first.id != second.id


async def test_create_requires_workspace() -> None:
    """Routes without an active workspace surface 403 rather than 500."""
    ctx = RequestContext(
        user_id="u1",
        workspace_id=None,
        request_id="t",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )
    with pytest.raises(Forbidden):
        await cycles_service.agent_create_cycle(
            ctx,
            CreateCycleRequest(
                name="x",
                start=date(2026, 5, 1),
                end=date(2026, 5, 29),
            ),
        )


# ---------------------------------------------------------------------------
# List + Get
# ---------------------------------------------------------------------------


async def test_list_sorts_by_status_then_start_desc() -> None:
    ctx = _ctx()
    await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="completed-old",
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            status="upcoming",
        ),
    )
    # Make one completed
    completed = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="completed-recent",
            start=date(2026, 4, 1),
            end=date(2026, 4, 30),
            status="upcoming",
        ),
    )
    await cycles_service.agent_close_cycle(ctx, completed.id)

    await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="upcoming-soon",
            pocket_id="p-future",
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
            status="upcoming",
        ),
    )
    await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="active-now",
            pocket_id="p-now",
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            status="active",
        ),
    )

    listing = await cycles_service.agent_list_cycles(ctx)
    statuses = [c.status for c in listing]
    # active first, then upcoming, then completed
    assert statuses[0] == "active"
    assert statuses[-1] == "completed"
    assert "upcoming" in statuses


async def test_list_is_tenant_scoped() -> None:
    """Cycles from another workspace never leak into the list."""
    await cycles_service.agent_create_cycle(
        _ctx(workspace="w1"),
        CreateCycleRequest(name="w1-cycle", start=date(2026, 5, 1), end=date(2026, 5, 31)),
    )
    await cycles_service.agent_create_cycle(
        _ctx(workspace="w2"),
        CreateCycleRequest(name="w2-cycle", start=date(2026, 5, 1), end=date(2026, 5, 31)),
    )
    listing_w1 = await cycles_service.agent_list_cycles(_ctx(workspace="w1"))
    listing_w2 = await cycles_service.agent_list_cycles(_ctx(workspace="w2"))
    assert {c.name for c in listing_w1} == {"w1-cycle"}
    assert {c.name for c in listing_w2} == {"w2-cycle"}


async def test_get_raises_not_found_for_other_workspace() -> None:
    ws1 = _ctx(workspace="w1")
    out = await cycles_service.agent_create_cycle(
        ws1,
        CreateCycleRequest(name="x", start=date(2026, 5, 1), end=date(2026, 5, 31)),
    )
    ws2 = _ctx(workspace="w2")
    with pytest.raises(NotFound):
        await cycles_service.agent_get_cycle(ws2, out.id)


async def test_get_returns_daily_series() -> None:
    ctx = _ctx()
    out = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="x", start=date(2026, 5, 1), end=date(2026, 5, 31), status="active"
        ),
    )
    fetched = await cycles_service.agent_get_cycle(ctx, out.id)
    assert fetched.daily == []  # Empty until snapshot job runs


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_update_works_on_upcoming(recording_bus) -> None:
    ctx = _ctx()
    out = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="orig",
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            status="upcoming",
        ),
    )
    recording_bus.events.clear()
    updated = await cycles_service.agent_update_cycle(
        ctx,
        out.id,
        UpdateCycleRequest(
            name="renamed",
            start=date(2026, 5, 5),
            end=date(2026, 6, 5),
        ),
    )
    assert updated.name == "renamed"
    assert updated.start == "2026-05-05"
    assert updated.end == "2026-06-05"
    assert any(isinstance(e, CycleUpdated) for e in recording_bus.events)


async def test_update_forbidden_on_active_cycle() -> None:
    ctx = _ctx()
    out = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="x",
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            status="active",
        ),
    )
    with pytest.raises(Forbidden):
        await cycles_service.agent_update_cycle(ctx, out.id, UpdateCycleRequest(name="renamed"))


async def test_update_rejects_inverted_dates() -> None:
    # The DTO validator catches inverted dates before they reach the service.
    with pytest.raises(ValueError):
        UpdateCycleRequest(start=date(2026, 6, 1), end=date(2026, 5, 1))


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


async def test_close_sets_status_and_emits(recording_bus) -> None:
    ctx = _ctx()
    out = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="x", start=date(2026, 5, 1), end=date(2026, 5, 31), status="active"
        ),
    )
    recording_bus.events.clear()
    closed = await cycles_service.agent_close_cycle(ctx, out.id)
    assert closed.status == "completed"
    closed_events = [e for e in recording_bus.events if isinstance(e, CycleClosed)]
    assert len(closed_events) == 1
    assert closed_events[0].data["id"] == out.id
    assert "rolled_count" in closed_events[0].data


async def test_close_idempotent() -> None:
    """Closing an already-completed cycle returns 409."""
    ctx = _ctx()
    out = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="x", start=date(2026, 5, 1), end=date(2026, 5, 31), status="active"
        ),
    )
    await cycles_service.agent_close_cycle(ctx, out.id)
    with pytest.raises(ConflictError):
        await cycles_service.agent_close_cycle(ctx, out.id)


async def test_close_rolls_incomplete_tasks() -> None:
    """Closing a cycle moves incomplete tasks to the next active cycle on
    the same pocket; ``done`` tasks stay attached to the closing cycle."""
    if not _tasks_available():
        pytest.skip(
            "Tasks entity not present on this branch — fork predates PR 2"
        )

    from ee.cloud.tasks import service as tasks_service
    from ee.cloud.tasks.dto import (
        AssigneeDTO,
        CompleteTaskRequest,
        CreateTaskRequest,
    )

    ctx = _ctx()
    # Closing cycle on pocket p1, with a follow-up active cycle on the
    # same pocket so the rollover has a target.
    closing = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="closing",
            pocket_id="p1",
            start=date(2026, 5, 1),
            end=date(2026, 5, 14),
            status="active",
        ),
    )
    follow_up = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="follow-up",
            pocket_id="p1",
            start=date(2026, 5, 15),
            end=date(2026, 5, 28),
            status="active",
        ),
    )

    # Two tasks attached to the closing cycle: one done (stays put), one
    # in-progress (rolls forward).
    done_task = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(
            title="done",
            assignee=AssigneeDTO(kind="human", id="u1", name="u1"),
            cycle_id=closing.id,
            pocket_id="p1",
        ),
    )
    await tasks_service.agent_complete_task(
        ctx, done_task.id, CompleteTaskRequest(next_action="archive")
    )
    incomplete_task = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(
            title="incomplete",
            assignee=AssigneeDTO(kind="human", id="u1", name="u1"),
            cycle_id=closing.id,
            pocket_id="p1",
        ),
    )

    closed = await cycles_service.agent_close_cycle(ctx, closing.id)
    assert closed.status == "completed"

    # The incomplete task moved to the follow-up cycle; the done task
    # stayed on the (now closed) original cycle.
    rolled = await tasks_service.agent_get_task(ctx, incomplete_task.id)
    assert rolled.cycle_id == follow_up.id
    stayed = await tasks_service.agent_get_task(ctx, done_task.id)
    assert stayed.cycle_id == closing.id


async def test_close_drops_to_unscheduled_when_no_follow_up() -> None:
    """Cycle close with no other active cycle on the same pocket clears
    the incomplete tasks' cycle_id instead of rolling forward."""
    if not _tasks_available():
        pytest.skip(
            "Tasks entity not present on this branch — fork predates PR 2"
        )

    from ee.cloud.tasks import service as tasks_service
    from ee.cloud.tasks.dto import AssigneeDTO, CreateTaskRequest

    ctx = _ctx()
    closing = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="closing-orphan",
            pocket_id="p-orphan",
            start=date(2026, 5, 1),
            end=date(2026, 5, 14),
            status="active",
        ),
    )
    orphan = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(
            title="orphan",
            assignee=AssigneeDTO(kind="human", id="u1", name="u1"),
            cycle_id=closing.id,
            pocket_id="p-orphan",
        ),
    )
    await cycles_service.agent_close_cycle(ctx, closing.id)
    fetched = await tasks_service.agent_get_task(ctx, orphan.id)
    assert fetched.cycle_id is None


# ---------------------------------------------------------------------------
# List items
# ---------------------------------------------------------------------------


async def test_list_items_empty_when_tasks_unavailable() -> None:
    """Graceful degradation: when Tasks isn't merged, items returns []."""
    ctx = _ctx()
    out = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(name="x", start=date(2026, 5, 1), end=date(2026, 5, 31)),
    )
    if _tasks_available():
        pytest.skip("Tasks entity is available — exercise full path in PR 2 suite")
    items = await cycles_service.agent_list_cycle_items(ctx, out.id)
    assert items == []


async def test_list_items_raises_not_found_for_other_workspace() -> None:
    out = await cycles_service.agent_create_cycle(
        _ctx(workspace="w1"),
        CreateCycleRequest(name="x", start=date(2026, 5, 1), end=date(2026, 5, 31)),
    )
    with pytest.raises(NotFound):
        await cycles_service.agent_list_cycle_items(_ctx(workspace="w2"), out.id)


# ---------------------------------------------------------------------------
# Lookup edge cases
# ---------------------------------------------------------------------------


async def test_get_invalid_id_raises_not_found() -> None:
    """Malformed cycle ids surface 404, not 500 from a bson parse error."""
    ctx = _ctx()
    with pytest.raises(NotFound):
        await cycles_service.agent_get_cycle(ctx, "not-a-real-id")
