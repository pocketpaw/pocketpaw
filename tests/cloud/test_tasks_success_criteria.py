# Created: 2026-05-21 — feat/taskspec-success-criteria. Tests for the
#   Tasks ``success_criteria`` / ``preconditions`` fields: create
#   persists them, omitting them defaults to an empty list, and they
#   round-trip through a subsequent get. These machine-verifiable
#   criteria are what completion-time verification (pocketpaw#1162)
#   reads off the Task.
"""Tasks ``success_criteria`` / ``preconditions`` — create + round-trip."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.tasks import service as tasks_service
from pocketpaw_ee.cloud.tasks.dto import AssigneeDTO, CreateTaskRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(*, user_id: str = "u-creator", workspace_id: str = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r-sc",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _human_assignee() -> AssigneeDTO:
    return AssigneeDTO(kind="human", id="u-creator", name="Creator")


async def test_create_with_criteria_persists_and_round_trips() -> None:
    """Both fields set on create come back on the response and on a
    subsequent get."""

    body = CreateTaskRequest(
        title="Build the health endpoint",
        assignee=_human_assignee(),
        success_criteria=[
            "GET /health returns HTTP 200",
            'the body is {"status":"ok"}',
        ],
        preconditions=["the FastAPI app is scaffolded"],
    )
    resp = await tasks_service.agent_create_task(_ctx(), body)
    assert resp.success_criteria == [
        "GET /health returns HTTP 200",
        'the body is {"status":"ok"}',
    ]
    assert resp.preconditions == ["the FastAPI app is scaffolded"]

    fetched = await tasks_service.agent_get_task(_ctx(), resp.id)
    assert fetched.success_criteria == [
        "GET /health returns HTTP 200",
        'the body is {"status":"ok"}',
    ]
    assert fetched.preconditions == ["the FastAPI app is scaffolded"]


async def test_create_without_criteria_defaults_to_empty() -> None:
    """Fields omitted on create → empty lists, not None. This is the
    backward-compatible path for callers predating the fields."""

    body = CreateTaskRequest(title="Standalone", assignee=_human_assignee())
    resp = await tasks_service.agent_create_task(_ctx(), body)
    assert resp.success_criteria == []
    assert resp.preconditions == []

    fetched = await tasks_service.agent_get_task(_ctx(), resp.id)
    assert fetched.success_criteria == []
    assert fetched.preconditions == []


async def test_success_criteria_independent_of_preconditions() -> None:
    """Setting one field does not bleed into the other."""

    body = CreateTaskRequest(
        title="Chase overdue invoices",
        assignee=_human_assignee(),
        success_criteria=["one reminder email per overdue account is sent"],
    )
    resp = await tasks_service.agent_create_task(_ctx(), body)
    assert resp.success_criteria == ["one reminder email per overdue account is sent"]
    assert resp.preconditions == []
