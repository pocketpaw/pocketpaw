# test_projects_service.py — service-level tests for the Projects entity.
# Created: 2026-05-16 — Mission Control backend completion. Covers CRUD,
#   tenant isolation, soft-archive idempotency, and the
#   cascade-unassign behaviour on delete.
"""Tests for ``ee.cloud.projects.service`` — CRUD + cascade unassign.

Exercises the service-level API directly against the shared
mongomock-motor fixture. The ``recording_bus`` autouse fixture captures
emitted events so we can verify the per-mutation event surface.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.events import (
    ProjectArchived,
    ProjectCreated,
    ProjectDeleted,
    ProjectUpdated,
)
from pocketpaw_ee.cloud.projects import service as projects_service
from pocketpaw_ee.cloud.projects.dto import (
    CreateProjectRequest,
    ListProjectsRequest,
    UpdateProjectRequest,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace: str | None = "w1", user: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_persists_with_workspace_and_creator(recording_bus) -> None:
    out = await projects_service.agent_create(
        _ctx(workspace="w1", user="shawn"),
        CreateProjectRequest(name="Q3 Launch", color="#0A84FF", description="big push"),
    )
    assert out.name == "Q3 Launch"
    assert out.workspace_id == "w1"
    assert out.created_by == "shawn"
    assert out.status == "active"
    assert out.color == "#0A84FF"

    created_events = [e for e in recording_bus.events if isinstance(e, ProjectCreated)]
    assert len(created_events) == 1
    assert created_events[0].data["project_id"] == out.id


async def test_create_requires_workspace() -> None:
    with pytest.raises(ValidationError):
        await projects_service.agent_create(
            _ctx(workspace=None),
            CreateProjectRequest(name="x"),
        )


# ---------------------------------------------------------------------------
# List + Get
# ---------------------------------------------------------------------------


async def test_list_is_tenant_scoped() -> None:
    await projects_service.agent_create(_ctx(workspace="w1"), CreateProjectRequest(name="A"))
    await projects_service.agent_create(_ctx(workspace="w2"), CreateProjectRequest(name="B"))
    listing_w1 = await projects_service.agent_list(_ctx(workspace="w1"))
    listing_w2 = await projects_service.agent_list(_ctx(workspace="w2"))
    assert {p.name for p in listing_w1} == {"A"}
    assert {p.name for p in listing_w2} == {"B"}


async def test_list_filters_by_status() -> None:
    ctx = _ctx()
    a = await projects_service.agent_create(ctx, CreateProjectRequest(name="active-one"))
    b = await projects_service.agent_create(ctx, CreateProjectRequest(name="to-archive"))
    await projects_service.agent_archive(ctx, b.id)

    active = await projects_service.agent_list(ctx, ListProjectsRequest(status="active"))
    assert {p.name for p in active} == {"active-one"}
    archived = await projects_service.agent_list(ctx, ListProjectsRequest(status="archived"))
    assert {p.name for p in archived} == {"to-archive"}

    # Default (no status) returns both
    everything = await projects_service.agent_list(ctx, ListProjectsRequest())
    assert {p.name for p in everything} == {"active-one", "to-archive"}

    # Keep referenced
    _ = a


async def test_get_raises_not_found_for_other_workspace() -> None:
    out = await projects_service.agent_create(_ctx(workspace="w1"), CreateProjectRequest(name="x"))
    with pytest.raises(NotFound):
        await projects_service.agent_get(_ctx(workspace="w2"), out.id)


async def test_get_invalid_id_raises_not_found() -> None:
    """Malformed ids surface 404, not a bson parse error."""
    with pytest.raises(NotFound):
        await projects_service.agent_get(_ctx(), "not-a-real-id")


# ---------------------------------------------------------------------------
# Update + Archive
# ---------------------------------------------------------------------------


async def test_update_changes_metadata(recording_bus) -> None:
    ctx = _ctx()
    out = await projects_service.agent_create(ctx, CreateProjectRequest(name="orig"))
    recording_bus.events.clear()
    updated = await projects_service.agent_update(
        ctx, out.id, UpdateProjectRequest(name="renamed", color="#FF00FF")
    )
    assert updated.name == "renamed"
    assert updated.color == "#FF00FF"
    assert any(isinstance(e, ProjectUpdated) for e in recording_bus.events)


async def test_archive_is_idempotent(recording_bus) -> None:
    ctx = _ctx()
    out = await projects_service.agent_create(ctx, CreateProjectRequest(name="x"))
    recording_bus.events.clear()
    a1 = await projects_service.agent_archive(ctx, out.id)
    assert a1.status == "archived"
    archived_events = [e for e in recording_bus.events if isinstance(e, ProjectArchived)]
    assert len(archived_events) == 1

    # Second archive is a no-op event-wise.
    a2 = await projects_service.agent_archive(ctx, out.id)
    assert a2.status == "archived"
    archived_events = [e for e in recording_bus.events if isinstance(e, ProjectArchived)]
    assert len(archived_events) == 1


# ---------------------------------------------------------------------------
# Delete + cascade unassign
# ---------------------------------------------------------------------------


async def test_delete_emits_event(recording_bus) -> None:
    ctx = _ctx()
    out = await projects_service.agent_create(ctx, CreateProjectRequest(name="x"))
    recording_bus.events.clear()
    await projects_service.agent_delete(ctx, out.id)
    with pytest.raises(NotFound):
        await projects_service.agent_get(ctx, out.id)
    assert any(isinstance(e, ProjectDeleted) for e in recording_bus.events)


async def test_delete_unassigns_pockets_tasks_cycles() -> None:
    """Deleting a project clears the ``project_id`` on every child row
    in the same workspace, but doesn't cascade-delete the children
    themselves — historical pockets / tasks / cycles stay alive."""
    from datetime import date

    from pocketpaw_ee.cloud.cycles import service as cycles_service
    from pocketpaw_ee.cloud.cycles.dto import CreateCycleRequest
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest
    from pocketpaw_ee.cloud.tasks import service as tasks_service
    from pocketpaw_ee.cloud.tasks.dto import AssigneeDTO, CreateTaskRequest

    ctx = _ctx()
    project = await projects_service.agent_create(ctx, CreateProjectRequest(name="P"))

    # Pocket — different signature (legacy), uses workspace_id + user_id.
    pocket_wire = await pockets_service.create(
        ctx.workspace_id,
        ctx.user_id,
        CreatePocketRequest(name="pock", project_id=project.id),
    )
    pocket_id = pocket_wire["_id"]

    task = await tasks_service.agent_create_task(
        ctx,
        CreateTaskRequest(
            title="t1",
            assignee=AssigneeDTO(kind="human", id="u1", name="u1"),
            project_id=project.id,
        ),
    )
    cycle = await cycles_service.agent_create_cycle(
        ctx,
        CreateCycleRequest(
            name="c1",
            project_id=project.id,
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            status="upcoming",
        ),
    )

    # Sanity: children carry the project_id pre-delete
    assert pocket_wire.get("projectId") == project.id
    assert task.project_id == project.id
    assert cycle.project_id == project.id

    await projects_service.agent_delete(ctx, project.id)

    # Children still exist…
    listing = await pockets_service.list_pockets(ctx.workspace_id, ctx.user_id)
    assert pocket_id in {p["_id"] for p in listing}
    task_after = await tasks_service.agent_get_task(ctx, task.id)
    cycle_after = await cycles_service.agent_get_cycle(ctx, cycle.id)

    # …but their project_id was cleared.
    surviving_pocket = next(p for p in listing if p["_id"] == pocket_id)
    assert surviving_pocket.get("projectId") is None
    assert task_after.project_id is None
    assert cycle_after.project_id is None


async def test_delete_aborts_if_cascade_unassign_fails(monkeypatch) -> None:
    """If any cascade-unassign step raises, the project row must NOT be
    deleted. Otherwise we'd leak orphaned ``project_id`` references on
    pockets/tasks/cycles. The whole operation is retry-safe only when
    the failure aborts the delete."""
    from pocketpaw_ee.cloud.tasks import service as tasks_service

    ctx = _ctx()
    project = await projects_service.agent_create(ctx, CreateProjectRequest(name="P"))

    async def boom(*_args, **_kwargs) -> None:
        raise RuntimeError("transient mongo error")

    monkeypatch.setattr(tasks_service, "unassign_project_on_tasks", boom)

    with pytest.raises(RuntimeError, match="transient mongo error"):
        await projects_service.agent_delete(ctx, project.id)

    # Project row must still exist — the caller can retry.
    surviving = await projects_service.agent_get(ctx, project.id)
    assert surviving.id == project.id


# ---------------------------------------------------------------------------
# Validation helper used by sibling services
# ---------------------------------------------------------------------------


async def test_exists_in_workspace_returns_true_only_for_match() -> None:
    out = await projects_service.agent_create(_ctx(workspace="w1"), CreateProjectRequest(name="x"))
    assert await projects_service.exists_in_workspace("w1", out.id) is True
    assert await projects_service.exists_in_workspace("w2", out.id) is False
    assert await projects_service.exists_in_workspace("w1", "not-a-real-id") is False
    assert await projects_service.exists_in_workspace("", out.id) is False
