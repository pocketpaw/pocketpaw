# test_tasks_calendar_bridge.py — Task → Calendar bridge integration.
# Created: 2026-08-06 (feat/coupling-tasks-on-calendar, coupling wave T-12).
#   End-to-end through the REAL tasks service, a REAL InProcessBus, and
#   the REAL calendar service against mongomock (the cloud mongo_db
#   fixture initializes the calendar Beanie docs via ALL_DOCUMENTS) —
#   the seam under test is the coupling itself, so mocking it would
#   prove nothing (spy-don't-mock). Docstrings name the mutation that
#   breaks each test; the plan lives in
#   tests/mutations/tasks_calendar_bridge.json.
"""Tests for the task.* → calendar bridge (tasks/bridges/calendar.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus
from pocketpaw_ee.cloud.tasks import service as tasks_service
from pocketpaw_ee.cloud.tasks.bridges.calendar import register_task_calendar_listeners
from pocketpaw_ee.cloud.tasks.dto import (
    AssigneeDTO,
    CompleteTaskRequest,
    CreateTaskRequest,
    UpdateTaskRequest,
)

pytestmark = pytest.mark.usefixtures("mongo_db")

DUE = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)


class _StubConnManager:
    async def send_to_user(self, user_id, payload) -> None:  # noqa: ARG002
        return None


@pytest_asyncio.fixture
async def real_bus():
    """Install a real InProcessBus wired with ONLY the calendar bridge.

    Overrides the autouse RecordingBus so the task.* subscribers actually
    fire. The notification listener is deliberately not registered — its
    fan-out is covered in test_tasks_notification_fanout.py.
    """

    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver

    async def _empty(_):
        return []

    resolver = AudienceResolver(
        group_members=_empty,
        workspace_members=_empty,
        workspace_admins=_empty,
        workspace_peers=_empty,
    )

    async def _no_audience(event):  # noqa: ARG001
        return []

    resolver.audience = _no_audience  # type: ignore[method-assign]

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus = InProcessBus(resolver=resolver, conn_manager=_StubConnManager())
    bus_mod._bus = bus  # type: ignore[attr-defined]
    register_task_calendar_listeners()
    try:
        yield bus
    finally:
        bus_mod._bus = prev  # type: ignore[attr-defined]


def _ctx(user_id: str = "creator-1", workspace_id: str = "ws-a") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _events_in(workspace_id: str) -> list:
    from pocketpaw_ee.calendar.models import _EventDoc

    return await _EventDoc.find({"workspace": workspace_id}).to_list()


async def _create_task(due_at=DUE, workspace_id: str = "ws-a", **overrides):
    body = CreateTaskRequest(
        title=overrides.pop("title", "Ship the audit report"),
        assignee=overrides.pop("assignee", AssigneeDTO(kind="human", id="creator-1", name="Me")),
        due_at=due_at,
        **overrides,
    )
    return await tasks_service.agent_create_task(_ctx(workspace_id=workspace_id), body)


# ---------------------------------------------------------------------------
# Create — due date lands on the calendar
# ---------------------------------------------------------------------------


async def test_task_with_due_date_appears_on_tasks_calendar(real_bus) -> None:
    """Creating a task with a due_at mints an event on the synthetic
    'tasks' calendar, timed at due_at, linked via fabric_object_id.

    Mutation that breaks this: bridge never calls create_event
    ('drop the create' in tests/mutations/tasks_calendar_bridge.json).
    """

    task = await _create_task()

    events = await _events_in("ws-a")
    assert len(events) == 1
    evt = events[0]
    assert evt.calendar_id == "tasks"
    assert evt.fabric_object_id == f"task:{task.id}"
    assert evt.title == "Ship the audit report"
    starts = evt.starts_at.replace(tzinfo=UTC) if evt.starts_at.tzinfo is None else evt.starts_at
    assert starts == DUE
    assert evt.ends_at > evt.starts_at


async def test_task_without_due_date_mints_nothing(real_bus) -> None:
    """No due_at → no calendar event. Most tasks are not deadlined."""

    await _create_task(due_at=None)
    assert await _events_in("ws-a") == []


async def test_duplicate_proposed_emit_converges_to_one_event(real_bus) -> None:
    """Re-delivering task.proposed for the same task must not duplicate
    the event — find-before-create on fabric_object_id dedupes."""

    from pocketpaw_ee.cloud._core.realtime.events import TaskProposed
    from pocketpaw_ee.cloud.tasks.bridges.calendar import _on_task_proposed

    task = await _create_task()
    # Replay the same payload the service emitted (bus re-delivery).
    await _on_task_proposed(
        TaskProposed(
            data={
                "task_id": task.id,
                "task": task.model_dump(),
                "workspace_id": "ws-a",
                "recipient_ids": [],
            }
        )
    )

    assert len(await _events_in("ws-a")) == 1


# ---------------------------------------------------------------------------
# Update — due date moves / clears
# ---------------------------------------------------------------------------


async def test_due_date_change_moves_the_event(real_bus) -> None:
    """Changing due_at moves the calendar event to the new time.

    Mutation that breaks this: bridge's update_event call becomes a
    no-op ('drop the update-on-change')."""

    task = await _create_task()
    new_due = DUE + timedelta(days=2)

    await tasks_service.agent_update_task(_ctx(), task.id, UpdateTaskRequest(due_at=new_due))

    events = await _events_in("ws-a")
    assert len(events) == 1
    starts = events[0].starts_at
    starts = starts.replace(tzinfo=UTC) if starts.tzinfo is None else starts
    assert starts == new_due


async def test_title_change_retitles_the_event(real_bus) -> None:
    """Renaming a deadlined task renames its calendar event too."""

    task = await _create_task()
    await tasks_service.agent_update_task(_ctx(), task.id, UpdateTaskRequest(title="New name"))

    events = await _events_in("ws-a")
    assert len(events) == 1
    assert events[0].title == "New name"


async def test_clearing_due_date_deletes_the_event(real_bus) -> None:
    """Explicit due_at=null removes the deadline → event disappears.

    Mutation that breaks this: bridge's delete_event call becomes a
    no-op ('drop the delete')."""

    task = await _create_task()
    assert len(await _events_in("ws-a")) == 1

    await tasks_service.agent_update_task(_ctx(), task.id, UpdateTaskRequest(due_at=None))

    assert await _events_in("ws-a") == []


async def test_adding_due_date_later_mints_the_event(real_bus) -> None:
    """A task created without a deadline gains one via update → the
    event appears (the reconciler converges from any starting state)."""

    task = await _create_task(due_at=None)
    assert await _events_in("ws-a") == []

    await tasks_service.agent_update_task(_ctx(), task.id, UpdateTaskRequest(due_at=DUE))

    events = await _events_in("ws-a")
    assert len(events) == 1
    assert events[0].fabric_object_id == f"task:{task.id}"


# ---------------------------------------------------------------------------
# Resolve — completing the task clears the calendar
# ---------------------------------------------------------------------------


async def test_completing_task_deletes_the_event(real_bus) -> None:
    """Archiving a deadlined task removes its calendar event.

    Mutations that break this: 'drop the delete' and 'terminal statuses
    stay calendar-eligible'."""

    task = await _create_task()
    assert len(await _events_in("ws-a")) == 1

    await tasks_service.agent_complete_task(
        _ctx(), task.id, CompleteTaskRequest(next_action="archive")
    )

    assert await _events_in("ws-a") == []


async def test_edit_while_awaiting_approval_does_not_resurrect_event(real_bus) -> None:
    """After completion into awaiting_approval, an unrelated metadata
    edit must NOT re-mint the event — awaiting_approval is not a
    calendar-eligible status."""

    task = await _create_task()
    await tasks_service.agent_complete_task(
        _ctx(), task.id, CompleteTaskRequest(next_action="request_approval")
    )
    assert await _events_in("ws-a") == []

    await tasks_service.agent_update_task(_ctx(), task.id, UpdateTaskRequest(summary="note"))

    assert await _events_in("ws-a") == []


# ---------------------------------------------------------------------------
# Tenancy — the event carries the task's workspace, and only that one
# ---------------------------------------------------------------------------


async def test_event_is_stamped_with_the_tasks_workspace(real_bus) -> None:
    """The minted event's workspace equals the task's workspace, and a
    lookup (or list) from another workspace sees nothing.

    Mutation that breaks this: bridge ctx hardcodes another workspace
    ('leak the workspace stamp')."""

    from pocketpaw_ee.calendar._context import RequestContext as CalCtx
    from pocketpaw_ee.calendar.dto import ListEventsRequest
    from pocketpaw_ee.calendar.service import list_events

    await _create_task(workspace_id="ws-a")

    events_a = await _events_in("ws-a")
    assert len(events_a) == 1
    assert events_a[0].workspace == "ws-a"
    # Nothing minted into any other tenant's calendar...
    assert await _events_in("ws-b") == []
    # ...and the calendar read surface for ws-b can't see it either.
    listed_b = await list_events(
        CalCtx(workspace_id="ws-b", user_id="intruder"),
        ListEventsRequest(
            starts_after=DUE - timedelta(days=30),
            starts_before=DUE + timedelta(days=30),
        ),
    )
    assert listed_b.events == []


# ---------------------------------------------------------------------------
# Containment — a broken calendar must never break the task flow
# ---------------------------------------------------------------------------


async def test_task_create_survives_calendar_failure(real_bus, monkeypatch) -> None:
    """When create_event raises, the task write still succeeds — the
    bridge contains the failure exactly like the meetings bridge does."""

    async def _boom(ctx, body):  # noqa: ARG001
        raise RuntimeError("calendar is down")

    monkeypatch.setattr("pocketpaw_ee.calendar.service.create_event", _boom)

    task = await _create_task()

    assert task.title == "Ship the audit report"  # write committed
    got = await tasks_service.agent_get_task(_ctx(), task.id)
    assert got.id == task.id
    assert await _events_in("ws-a") == []  # no event, but no crash either


async def test_task_complete_survives_calendar_failure(real_bus, monkeypatch) -> None:
    """When delete_event raises, completing the task still succeeds."""

    task = await _create_task()

    async def _boom(ctx, event_id):  # noqa: ARG001
        raise RuntimeError("calendar is down")

    monkeypatch.setattr("pocketpaw_ee.calendar.service.delete_event", _boom)

    done = await tasks_service.agent_complete_task(
        _ctx(), task.id, CompleteTaskRequest(next_action="archive")
    )
    assert done.status == "done"


async def test_comment_emit_with_task_none_is_a_noop(real_bus) -> None:
    """task.updated fired for a task comment ships task=None — the
    bridge must treat it as a no-op, not a crash."""

    from pocketpaw_ee.cloud._core.realtime.events import TaskUpdated
    from pocketpaw_ee.cloud.tasks.bridges.calendar import _on_task_updated

    await _on_task_updated(
        TaskUpdated(
            data={
                "task_id": "t-1",
                "task": None,
                "workspace_id": "ws-a",
                "recipient_ids": [],
            }
        )
    )

    assert await _events_in("ws-a") == []
