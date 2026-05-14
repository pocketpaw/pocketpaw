# test_tasks_service.py — service-layer tests for the Tasks entity.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend. Exercises
#   CRUD, status transitions, tenancy isolation, and the emit-on-write
#   guarantee. Uses the shared ``mongo_db`` fixture so real Beanie writes
#   land in mongomock-motor. The ``recording_bus`` autouse fixture
#   captures emitted events for assertion.
"""Service-layer tests for the Tasks entity."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from ee.cloud._core.realtime.events import (
    TaskBlocked,
    TaskProposed,
    TaskResolved,
    TaskUpdated,
)
from ee.cloud.tasks import service as tasks_service
from ee.cloud.tasks.dto import (
    AssigneeDTO,
    BlockTaskRequest,
    CompleteTaskRequest,
    CreateTaskRequest,
    ListTasksRequest,
    ReassignTaskRequest,
    SourceDTO,
    UpdateTaskRequest,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(*, user_id: str = "u-creator", workspace_id: str = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r1",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _human_assignee(user_id: str = "u-assignee", name: str = "Jess") -> AssigneeDTO:
    return AssigneeDTO(kind="human", id=user_id, name=name)


def _agent_assignee(agent_id: str = "agent-events", name: str = "events-agent") -> AssigneeDTO:
    return AssigneeDTO(kind="agent", id=agent_id, name=name)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_for_agent_defaults_to_proposed(recording_bus) -> None:
    body = CreateTaskRequest(title="Draft run-of-show", assignee=_agent_assignee())
    resp = await tasks_service.agent_create_task(_ctx(), body)
    assert resp.status == "proposed"
    assert resp.assignee.kind == "agent"
    assert resp.workspace_id == "w1"
    assert resp.creator_id == "u-creator"

    proposed_events = [e for e in recording_bus.events if isinstance(e, TaskProposed)]
    assert len(proposed_events) == 1
    assert proposed_events[0].data["task"]["status"] == "proposed"


async def test_create_for_human_defaults_to_in_progress() -> None:
    body = CreateTaskRequest(title="Reply to vendor", assignee=_human_assignee(), priority="high")
    resp = await tasks_service.agent_create_task(_ctx(), body)
    assert resp.status == "in_progress"
    assert resp.priority == "high"
    assert resp.assignee.kind == "human"


async def test_create_requires_workspace() -> None:
    body = CreateTaskRequest(title="x", assignee=_agent_assignee())
    no_ws = _ctx(workspace_id=None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await tasks_service.agent_create_task(no_ws, body)


async def test_create_persists_source_metadata() -> None:
    body = CreateTaskRequest(
        title="Approve change",
        assignee=_human_assignee(),
        kind="nudge",
        source=SourceDTO(type="nudge", ref_id="nudge-1", metadata={"reason": "policy"}),
    )
    resp = await tasks_service.agent_create_task(_ctx(), body)
    assert resp.source.type == "nudge"
    assert resp.source.ref_id == "nudge-1"
    assert resp.source.metadata == {"reason": "policy"}
    assert resp.kind == "nudge"


# ---------------------------------------------------------------------------
# List + Get
# ---------------------------------------------------------------------------


async def test_list_filters_by_workspace() -> None:
    await tasks_service.agent_create_task(
        _ctx(workspace_id="w1"),
        CreateTaskRequest(title="t1", assignee=_human_assignee()),
    )
    await tasks_service.agent_create_task(
        _ctx(workspace_id="w2"),
        CreateTaskRequest(title="t2", assignee=_human_assignee()),
    )
    listed_w1 = await tasks_service.agent_list_tasks(_ctx(workspace_id="w1"), ListTasksRequest())
    assert {t.title for t in listed_w1} == {"t1"}
    listed_w2 = await tasks_service.agent_list_tasks(_ctx(workspace_id="w2"), ListTasksRequest())
    assert {t.title for t in listed_w2} == {"t2"}


async def test_list_filters_by_assignee_and_status() -> None:
    ctx = _ctx()
    await tasks_service.agent_create_task(
        ctx, CreateTaskRequest(title="agent-task", assignee=_agent_assignee("agent-a"))
    )
    await tasks_service.agent_create_task(
        ctx, CreateTaskRequest(title="agent-other", assignee=_agent_assignee("agent-b"))
    )
    await tasks_service.agent_create_task(
        ctx, CreateTaskRequest(title="human-task", assignee=_human_assignee("u-jess"))
    )

    only_agent_a = await tasks_service.agent_list_tasks(
        ctx, ListTasksRequest(assignee_id="agent-a")
    )
    assert [t.title for t in only_agent_a] == ["agent-task"]

    only_proposed = await tasks_service.agent_list_tasks(ctx, ListTasksRequest(status="proposed"))
    assert {t.title for t in only_proposed} == {"agent-task", "agent-other"}

    only_human = await tasks_service.agent_list_tasks(ctx, ListTasksRequest(assignee_kind="human"))
    assert [t.title for t in only_human] == ["human-task"]


async def test_get_tenancy_isolation() -> None:
    created = await tasks_service.agent_create_task(
        _ctx(workspace_id="w1"),
        CreateTaskRequest(title="secret", assignee=_human_assignee()),
    )
    # Same task id, different workspace context → NotFound (not Forbidden).
    with pytest.raises(NotFound):
        await tasks_service.agent_get_task(_ctx(workspace_id="w2"), created.id)


async def test_get_returns_full_shape() -> None:
    created = await tasks_service.agent_create_task(
        _ctx(),
        CreateTaskRequest(title="t", summary="s", assignee=_human_assignee()),
    )
    fetched = await tasks_service.agent_get_task(_ctx(), created.id)
    assert fetched.id == created.id
    assert fetched.title == "t"
    assert fetched.summary == "s"


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


async def test_update_patches_only_provided_fields(recording_bus) -> None:
    created = await tasks_service.agent_create_task(
        _ctx(),
        CreateTaskRequest(
            title="old",
            summary="old",
            priority="normal",
            assignee=_human_assignee(),
        ),
    )
    recording_bus.events.clear()
    updated = await tasks_service.agent_update_task(
        _ctx(),
        created.id,
        UpdateTaskRequest(title="new", priority="urgent"),
    )
    assert updated.title == "new"
    assert updated.summary == "old"  # untouched
    assert updated.priority == "urgent"
    assert any(isinstance(e, TaskUpdated) for e in recording_bus.events)


async def test_update_denies_other_workspace_member() -> None:
    created = await tasks_service.agent_create_task(
        _ctx(user_id="creator"),
        CreateTaskRequest(title="x", assignee=_human_assignee("assignee")),
    )
    with pytest.raises(Forbidden):
        await tasks_service.agent_update_task(
            _ctx(user_id="random-stranger"),
            created.id,
            UpdateTaskRequest(title="hijack"),
        )


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


async def test_complete_archive_flips_to_done(recording_bus) -> None:
    created = await tasks_service.agent_create_task(
        _ctx(), CreateTaskRequest(title="t", assignee=_human_assignee())
    )
    recording_bus.events.clear()
    resp = await tasks_service.agent_complete_task(
        _ctx(), created.id, CompleteTaskRequest(next_action="archive")
    )
    assert resp.status == "done"
    assert any(isinstance(e, TaskResolved) for e in recording_bus.events)


async def test_complete_request_approval_routes_to_awaiting_approval() -> None:
    created = await tasks_service.agent_create_task(
        _ctx(), CreateTaskRequest(title="t", assignee=_agent_assignee())
    )
    resp = await tasks_service.agent_complete_task(
        _ctx(),
        created.id,
        CompleteTaskRequest(next_action="request_approval", result_summary="draft attached"),
    )
    assert resp.status == "awaiting_approval"
    assert "draft attached" in resp.summary


async def test_complete_rejects_terminal_state() -> None:
    created = await tasks_service.agent_create_task(
        _ctx(), CreateTaskRequest(title="t", assignee=_human_assignee())
    )
    await tasks_service.agent_complete_task(
        _ctx(), created.id, CompleteTaskRequest(next_action="archive")
    )
    with pytest.raises(ValidationError):
        await tasks_service.agent_complete_task(
            _ctx(), created.id, CompleteTaskRequest(next_action="archive")
        )


async def test_block_records_reason_and_emits(recording_bus) -> None:
    created = await tasks_service.agent_create_task(
        _ctx(), CreateTaskRequest(title="t", assignee=_agent_assignee())
    )
    recording_bus.events.clear()
    resp = await tasks_service.agent_block_task(
        _ctx(), created.id, BlockTaskRequest(reason="waiting on vendor confirm")
    )
    assert resp.status == "blocked"
    assert resp.blocked_reason == "waiting on vendor confirm"
    assert any(isinstance(e, TaskBlocked) for e in recording_bus.events)


async def test_reassign_changes_assignee_polymorphically(recording_bus) -> None:
    created = await tasks_service.agent_create_task(
        _ctx(), CreateTaskRequest(title="t", assignee=_human_assignee("u-shawn", "Shawn"))
    )
    recording_bus.events.clear()
    resp = await tasks_service.agent_reassign_task(
        _ctx(),
        created.id,
        ReassignTaskRequest(
            assignee_kind="agent",
            assignee_id="agent-events",
            assignee_name="events-agent",
        ),
    )
    assert resp.assignee.kind == "agent"
    assert resp.assignee.id == "agent-events"
    assert resp.assignee.name == "events-agent"
    assert any(isinstance(e, TaskUpdated) for e in recording_bus.events)


# ---------------------------------------------------------------------------
# list_for_agent_runtime — MCP tool surface
# ---------------------------------------------------------------------------


async def test_list_for_agent_runtime_filters_to_agent_id() -> None:
    ctx = _ctx()
    await tasks_service.agent_create_task(
        ctx, CreateTaskRequest(title="mine", assignee=_agent_assignee("agent-a"))
    )
    await tasks_service.agent_create_task(
        ctx, CreateTaskRequest(title="other", assignee=_agent_assignee("agent-b"))
    )
    await tasks_service.agent_create_task(
        ctx, CreateTaskRequest(title="human", assignee=_human_assignee())
    )
    mine = await tasks_service.list_for_agent_runtime(workspace_id="w1", agent_id="agent-a")
    assert [t.title for t in mine] == ["mine"]


async def test_list_for_agent_runtime_status_filter() -> None:
    ctx = _ctx()
    await tasks_service.agent_create_task(
        ctx, CreateTaskRequest(title="proposed", assignee=_agent_assignee("agent-a"))
    )
    # Mock an already-claimed one by creating then claiming
    created = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(title="in-progress", assignee=_agent_assignee("agent-a")),
    )
    from ee.cloud.tasks.dto import ClaimTaskRequest

    result = await tasks_service.agent_claim_task(
        ctx, created.id, ClaimTaskRequest(agent_id="agent-a")
    )
    assert result["ok"] is True

    proposed = await tasks_service.list_for_agent_runtime(
        workspace_id="w1", agent_id="agent-a", status="proposed"
    )
    assert {t.title for t in proposed} == {"proposed"}

    in_progress = await tasks_service.list_for_agent_runtime(
        workspace_id="w1", agent_id="agent-a", status="in_progress"
    )
    assert {t.title for t in in_progress} == {"in-progress"}
