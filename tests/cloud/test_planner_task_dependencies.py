# Created: 2026-05-17 — pocketpaw#1118 P4. Tests for the planner's
#   two-pass task materialization — the part that wires
#   ``TaskSpec.blocked_by_keys`` into cloud ``Task.blocked_by`` after
#   every sibling task exists. Forward references and unknown deps are
#   the failure modes the two-pass refactor exists to handle, so each
#   case has its own test.
"""Service-level coverage for the planner's two-pass dependency wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud.planner import service as planner_service
from ee.cloud.planner.dto import PlanProjectRequest
from ee.cloud.projects import service as projects_service
from ee.cloud.projects.dto import CreateProjectRequest
from ee.cloud.tasks import service as tasks_service
from ee.cloud.tasks.dto import ListTasksRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(*, user_id: str = "u-creator", workspace_id: str = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r-deps",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


class _FakeTaskSpec:
    def __init__(
        self,
        *,
        key: str,
        title: str = "",
        description: str = "",
        task_type: str = "agent",
        priority: str = "medium",
        required_specialties: list[str] | None = None,
        blocked_by_keys: list[str] | None = None,
    ) -> None:
        self.key = key
        self.title = title or f"Task {key}"
        self.description = description
        self.task_type = task_type
        self.priority = priority
        self.required_specialties = required_specialties or []
        self.blocked_by_keys = blocked_by_keys or []

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title}


class _FakePlannerResult:
    def __init__(
        self,
        *,
        tasks: list[_FakeTaskSpec] | None = None,
        team_recommendation: list | None = None,
    ) -> None:
        self.prd_content = "# PRD"
        self.tasks = tasks or []
        self.human_tasks: list = []
        self.team_recommendation = team_recommendation or []
        self.project_id = ""

    def to_dict(self) -> dict:
        return {"tasks": [t.to_dict() for t in self.tasks]}


def _patched_planner(result: _FakePlannerResult):
    async def _fake_plan(self, project_description, project_id="", research_depth="standard"):  # noqa: ARG001
        result.project_id = project_id
        return result

    return patch("pocketpaw.deep_work.planner.PlannerAgent.plan", new=_fake_plan)


@pytest.fixture(autouse=True)
def _isolated_upload_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


async def _make_project() -> str:
    proj = await projects_service.agent_create(_ctx(), CreateProjectRequest(name="Crestline"))
    return proj.id


async def _tasks_by_planner_key(project_id: str) -> dict[str, str]:
    """Map ``TaskSpec.key`` (carried via ``source.metadata.planner_task_key``)
    back to the cloud Task id so the assertions can read 'task B depends
    on task A' rather than juggling ObjectId strings."""

    rows = await tasks_service.agent_list_tasks(
        _ctx(), ListTasksRequest(project_id=project_id, limit=500)
    )
    out: dict[str, str] = {}
    for r in rows:
        key = (r.source.metadata or {}).get("planner_task_key", "")
        if key:
            out[key] = r.id
    return out


# ---------------------------------------------------------------------------
# Two-pass wiring
# ---------------------------------------------------------------------------


async def test_planner_materializes_dependencies_in_two_passes() -> None:
    """TaskSpec A depends on B; both materialize; A.blocked_by contains
    B's cloud id (not the spec key)."""

    project_id = await _make_project()
    fake = _FakePlannerResult(
        tasks=[
            _FakeTaskSpec(key="b", title="Build vendor list"),
            _FakeTaskSpec(key="a", title="Outreach", blocked_by_keys=["b"]),
        ],
    )
    with _patched_planner(fake):
        result = await planner_service.agent_plan_project(
            _ctx(), PlanProjectRequest(project_id=project_id, goal="Plan a thing carefully.")
        )

    assert result.dependency_warnings == []
    by_key = await _tasks_by_planner_key(project_id)
    task_a = await tasks_service.agent_get_task(_ctx(), by_key["a"])
    task_b = await tasks_service.agent_get_task(_ctx(), by_key["b"])
    assert task_a.blocked_by == [task_b.id]
    assert task_b.blocked_by == []


async def test_planner_handles_forward_references() -> None:
    """TaskSpec A is in the spec list BEFORE its dependency B. The
    two-pass refactor exists specifically so this case resolves cleanly
    instead of dropping A's dependency."""

    project_id = await _make_project()
    fake = _FakePlannerResult(
        tasks=[
            _FakeTaskSpec(key="a", title="Outreach", blocked_by_keys=["b"]),
            _FakeTaskSpec(key="b", title="Build vendor list"),
        ],
    )
    with _patched_planner(fake):
        result = await planner_service.agent_plan_project(
            _ctx(), PlanProjectRequest(project_id=project_id, goal="Plan a thing carefully.")
        )

    assert result.dependency_warnings == []
    by_key = await _tasks_by_planner_key(project_id)
    task_a = await tasks_service.agent_get_task(_ctx(), by_key["a"])
    task_b = await tasks_service.agent_get_task(_ctx(), by_key["b"])
    assert task_a.blocked_by == [task_b.id]


async def test_planner_skips_unknown_dep_names_with_warning() -> None:
    """TaskSpec A depends on a nonexistent spec X; A is still created
    with an empty blocked_by, and 'X' surfaces in dependency_warnings.
    This matches the brief — never cascade-delete tasks for a missing
    dep, just record the bug."""

    project_id = await _make_project()
    fake = _FakePlannerResult(
        tasks=[
            _FakeTaskSpec(key="a", title="Outreach", blocked_by_keys=["never-existed"]),
        ],
    )
    with _patched_planner(fake):
        result = await planner_service.agent_plan_project(
            _ctx(), PlanProjectRequest(project_id=project_id, goal="Plan a thing carefully.")
        )

    assert "never-existed" in result.dependency_warnings
    by_key = await _tasks_by_planner_key(project_id)
    task_a = await tasks_service.agent_get_task(_ctx(), by_key["a"])
    assert task_a.blocked_by == []
