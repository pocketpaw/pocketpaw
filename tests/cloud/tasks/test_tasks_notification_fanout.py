# test_tasks_notification_fanout.py — Tasks → notifications integration.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend. Exercises
#   the in-process bus subscriber that creates a Notification when a
#   Task is proposed to a human assignee. Agent assignees must not
#   trigger notifications (they poll their own queue).
"""Tests for the ``task.proposed`` → notification fan-out listener."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus
from pocketpaw_ee.cloud.notifications import service as notifications_service
from pocketpaw_ee.cloud.tasks import service as tasks_service
from pocketpaw_ee.cloud.tasks.dto import AssigneeDTO, CreateTaskRequest
from pocketpaw_ee.cloud.tasks.listeners import register_task_listeners

pytestmark = pytest.mark.usefixtures("mongo_db")


class _StubConnManager:
    async def send_to_user(self, user_id, payload) -> None:  # noqa: ARG002
        return None


class _StubResolver(AudienceResolver):
    """Audience resolver that returns no WebSocket recipients.

    Mission Control's fan-out behaviour is tested in the realtime
    package; here we only care that the in-process subscriber fires.
    """

    def __init__(self) -> None:
        async def _empty(_):
            return []

        super().__init__(
            group_members=_empty,
            workspace_members=_empty,
            workspace_admins=_empty,
            workspace_peers=_empty,
        )

    async def audience(self, event):  # type: ignore[override]
        return []


@pytest_asyncio.fixture
async def real_bus():
    """Install a real InProcessBus so the ``task.proposed`` subscriber
    actually fires. Overrides the autouse RecordingBus from the cloud
    conftest for the duration of the test."""

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus = InProcessBus(resolver=_StubResolver(), conn_manager=_StubConnManager())
    bus_mod._bus = bus  # type: ignore[attr-defined]
    register_task_listeners()
    try:
        yield bus
    finally:
        bus_mod._bus = prev  # type: ignore[attr-defined]


def _ctx(user_id: str = "creator-1", workspace_id: str = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_human_assignee_proposal_creates_notification(real_bus) -> None:
    """Routing a Task to a human assignee fires the listener and a
    notification appears in the recipient's inbox."""

    await tasks_service.agent_create_task(
        _ctx(user_id="creator-1"),
        CreateTaskRequest(
            title="Approve vendor change",
            assignee=AssigneeDTO(kind="human", id="u-jess", name="Jess"),
            kind="nudge",
        ),
    )

    notes = await notifications_service.list_for_user("u-jess")
    assert len(notes) == 1
    assert notes[0].kind == "task_assigned"
    assert notes[0].title == "Approve vendor change"
    assert notes[0].workspace_id == "w1"


async def test_agent_assignee_proposal_does_not_create_notification(real_bus) -> None:
    """Agent assignees do not trigger notification rows — they poll
    their own queue. The notification surface is human-only."""

    await tasks_service.agent_create_task(
        _ctx(user_id="creator-1"),
        CreateTaskRequest(
            title="Draft run-of-show",
            assignee=AssigneeDTO(kind="agent", id="agent-events", name="events-agent"),
        ),
    )

    notes_agent = await notifications_service.list_for_user("agent-events")
    assert notes_agent == []
    notes_creator = await notifications_service.list_for_user("creator-1")
    assert notes_creator == []


async def test_self_assigned_human_task_skips_notification(real_bus) -> None:
    """If the creator assigns a Task to themselves, the listener skips
    the notification — they already know about it, and Mission Control
    will surface it via the realtime push regardless."""

    await tasks_service.agent_create_task(
        _ctx(user_id="solo-shawn"),
        CreateTaskRequest(
            title="Call vendor",
            assignee=AssigneeDTO(kind="human", id="solo-shawn", name="Shawn"),
        ),
    )

    notes = await notifications_service.list_for_user("solo-shawn")
    assert notes == []


async def test_each_human_assignment_creates_one_notification(real_bus) -> None:
    """Distinct tasks routed to the same human assignee result in
    one notification per task — fan-out is per-event, not deduped."""

    ctx = _ctx(user_id="creator-1")
    await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(title="t1", assignee=AssigneeDTO(kind="human", id="u-jess")),
    )
    await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(title="t2", assignee=AssigneeDTO(kind="human", id="u-jess")),
    )

    notes = await notifications_service.list_for_user("u-jess")
    titles = {n.title for n in notes}
    assert titles == {"t1", "t2"}
