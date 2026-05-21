# Created: 2026-05-17 — pocketpaw#1118 P4. Tests for the Tasks
#   ``blocked_by`` field — create persists, update obeys tri-state
#   semantics (None = no change, [] = explicit clear, [...] = replace).
#   The planner relies on these guarantees for its two-pass dependency
#   wiring; manual callers do too.
"""Tasks ``blocked_by`` field — create + update tri-state semantics."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.tasks import service as tasks_service
from pocketpaw_ee.cloud.tasks.dto import (
    AssigneeDTO,
    CreateTaskRequest,
    UpdateTaskRequest,
)

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(*, user_id: str = "u-creator", workspace_id: str = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r-bb",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _human_assignee() -> AssigneeDTO:
    return AssigneeDTO(kind="human", id="u-creator", name="Creator")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_create_with_blocked_by_persists() -> None:
    """Round-trip — the ids set on create come back on the response and
    on a subsequent get."""

    body = CreateTaskRequest(
        title="Outreach",
        assignee=_human_assignee(),
        blocked_by=["task-abc", "task-def"],
    )
    resp = await tasks_service.agent_create_task(_ctx(), body)
    assert resp.blocked_by == ["task-abc", "task-def"]

    fetched = await tasks_service.agent_get_task(_ctx(), resp.id)
    assert fetched.blocked_by == ["task-abc", "task-def"]


async def test_create_with_no_blocked_by_defaults_to_empty() -> None:
    """Field omitted on create → empty list, not None."""

    body = CreateTaskRequest(title="Standalone", assignee=_human_assignee())
    resp = await tasks_service.agent_create_task(_ctx(), body)
    assert resp.blocked_by == []


# ---------------------------------------------------------------------------
# Update tri-state
# ---------------------------------------------------------------------------


async def test_update_clears_blocked_by_when_empty_list() -> None:
    """Explicit clear semantics: an empty list MUST overwrite stored
    deps so the operator can "unblock" a task in one call."""

    created = await tasks_service.agent_create_task(
        _ctx(),
        CreateTaskRequest(
            title="x",
            assignee=_human_assignee(),
            blocked_by=["task-1", "task-2"],
        ),
    )
    assert created.blocked_by == ["task-1", "task-2"]

    updated = await tasks_service.agent_update_task(
        _ctx(), created.id, UpdateTaskRequest(blocked_by=[])
    )
    assert updated.blocked_by == []
    refetched = await tasks_service.agent_get_task(_ctx(), created.id)
    assert refetched.blocked_by == []


async def test_update_leaves_blocked_by_alone_when_omitted() -> None:
    """None = no change. The DTO's ``blocked_by`` defaults to ``None``
    so a caller patching unrelated fields doesn't accidentally clear
    deps."""

    created = await tasks_service.agent_create_task(
        _ctx(),
        CreateTaskRequest(
            title="x",
            assignee=_human_assignee(),
            blocked_by=["task-1"],
        ),
    )
    # Patch an unrelated field; blocked_by stays put.
    updated = await tasks_service.agent_update_task(
        _ctx(), created.id, UpdateTaskRequest(title="new title")
    )
    assert updated.blocked_by == ["task-1"]
    assert updated.title == "new title"


async def test_update_replaces_blocked_by_with_new_list() -> None:
    """A non-empty list overwrites the stored set wholesale — there is
    no merge semantic. Callers wanting append must read-merge-write."""

    created = await tasks_service.agent_create_task(
        _ctx(),
        CreateTaskRequest(
            title="x",
            assignee=_human_assignee(),
            blocked_by=["task-1"],
        ),
    )
    updated = await tasks_service.agent_update_task(
        _ctx(), created.id, UpdateTaskRequest(blocked_by=["task-2", "task-3"])
    )
    assert updated.blocked_by == ["task-2", "task-3"]
