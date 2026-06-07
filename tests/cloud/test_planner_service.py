# Created: 2026-05-17 — pocketpaw#1118 P1. Service-level tests for the
#   planner entity. Mocks the OSS PlannerAgent so the test isolates the
#   *materialization* layer — the OSS planner itself has its own test
#   suite under ``tests/test_deep_work_planner.py`` and we should not
#   re-test it here.
"""Tests for ``ee.cloud.planner.service`` — happy path + agent-gap +
tenant guard.

Exercises the service against the shared mongomock-motor fixture with
the OSS ``PlannerAgent.plan`` patched to a deterministic result. The
``recording_bus`` autouse fixture captures the PlanGenerated +
TaskProposed + FileReady events emitted by the materialization chain.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.events import (
    FileReady,
    PlanGenerated,
    TaskProposed,
)
from pocketpaw_ee.cloud.planner import service as planner_service
from pocketpaw_ee.cloud.planner.dto import PlanProjectRequest
from pocketpaw_ee.cloud.projects import service as projects_service
from pocketpaw_ee.cloud.projects.dto import CreateProjectRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(*, user_id: str = "u-creator", workspace_id: str | None = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r1",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


class _FakeTaskSpec:
    """Stand-in for ``pocketpaw.deep_work.models.TaskSpec``.

    The service only reads attributes (no isinstance checks), so a duck
    type with the same field surface is enough to drive the
    materialization layer without importing the OSS dataclass.
    """

    def __init__(
        self,
        *,
        key: str = "research",
        title: str = "Research",
        description: str = "Do research",
        task_type: str = "agent",
        priority: str = "high",
        required_specialties: list[str] | None = None,
    ) -> None:
        self.key = key
        self.title = title
        self.description = description
        self.task_type = task_type
        self.priority = priority
        self.required_specialties = required_specialties or []
        self.blocked_by_keys: list[str] = []

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "task_type": self.task_type,
        }


class _FakeAgentSpec:
    def __init__(self, *, name: str, role: str = "", specialties: list[str] | None = None) -> None:
        self.name = name
        self.role = role
        self.specialties = specialties or []
        self.backend = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "role": self.role}


class _FakePlannerResult:
    """Stand-in for ``pocketpaw.deep_work.models.PlannerResult``."""

    def __init__(
        self,
        *,
        prd_content: str = "# PRD\n\nReal content",
        tasks: list[_FakeTaskSpec] | None = None,
        human_tasks: list[_FakeTaskSpec] | None = None,
        team_recommendation: list[_FakeAgentSpec] | None = None,
    ) -> None:
        self.prd_content = prd_content
        self.tasks = tasks or []
        self.human_tasks = human_tasks or []
        self.team_recommendation = team_recommendation or []
        self.dependency_graph: dict = {}
        self.research_notes = ""
        self.project_id = ""
        self.estimated_total_minutes = 0

    def to_dict(self) -> dict:
        return {
            "prd_content": self.prd_content,
            "tasks": [t.to_dict() for t in self.tasks],
            "team_recommendation": [a.to_dict() for a in self.team_recommendation],
        }


def _patched_planner(result: _FakePlannerResult, *, raises: Exception | None = None):
    """Patch ``PlannerAgent.plan`` to return ``result`` (or raise).

    The service constructs its own ``PlannerAgent`` inside ``_run_oss_planner``,
    so we patch the class method rather than an instance.
    """

    async def _fake_plan(self, project_description, project_id="", research_depth="standard"):  # noqa: ARG001
        if raises:
            raise raises
        result.project_id = project_id
        return result

    return patch(
        "pocketpaw.deep_work.planner.PlannerAgent.plan",
        new=_fake_plan,
    )


@pytest.fixture(autouse=True)
def _isolated_upload_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ``Path.home()`` so the planner's file writes land in
    ``tmp_path`` rather than the developer's real home directory.

    ``write_text_file`` resolves storage to ``~/.pocketpaw/uploads`` —
    monkeypatching Path.home keeps every test run hermetic without
    threading a tmp_path arg through the production code.
    """

    monkeypatch.setattr(Path, "home", lambda: tmp_path)


async def _make_project(*, name: str = "Crestline", lead_id: str = "u-lead") -> str:
    proj = await projects_service.agent_create(
        _ctx(),
        CreateProjectRequest(name=name, lead_id=lead_id),
    )
    return proj.id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_plan_project_writes_files_and_creates_tasks(recording_bus) -> None:
    project_id = await _make_project()
    fake = _FakePlannerResult(
        prd_content="# Plan\nReal PRD",
        tasks=[
            _FakeTaskSpec(key="research", title="Research vendors"),
            _FakeTaskSpec(key="outreach", title="Reach out", priority="medium"),
        ],
        team_recommendation=[
            _FakeAgentSpec(name="events-coordinator", role="Events Lead"),
        ],
    )

    with _patched_planner(fake):
        result = await planner_service.agent_plan_project(
            _ctx(),
            PlanProjectRequest(
                project_id=project_id,
                goal="Plan the Crestline wedding for May 23.",
                deep_research=False,
            ),
        )

    assert result.project_id == project_id
    assert result.status == "ready"
    assert result.prd_file_id, "PRD file id should be populated"
    assert result.goal_file_id, "goal.md file id should be populated"
    assert result.plan_file_id, "plan.json file id should be populated"
    assert len(result.task_ids) == 2

    # Each materialized task fired a TaskProposed (cloud assigns to the
    # project lead as human fallback because no cloud Agent matches yet).
    task_proposed = [e for e in recording_bus.events if isinstance(e, TaskProposed)]
    assert len(task_proposed) == 2

    # Three file writes → three FileReady events (PRD + goal + plan.json).
    file_ready = [e for e in recording_bus.events if isinstance(e, FileReady)]
    assert len(file_ready) == 3

    # Exactly one PlanGenerated at the end.
    plan_events = [e for e in recording_bus.events if isinstance(e, PlanGenerated)]
    assert len(plan_events) == 1
    assert plan_events[0].data["project_id"] == project_id
    assert plan_events[0].data["task_count"] == 2
    assert plan_events[0].data["agent_gap_count"] == 1


async def test_plan_project_surfaces_agent_gaps(recording_bus) -> None:
    project_id = await _make_project()
    fake = _FakePlannerResult(
        prd_content="# Plan",
        tasks=[_FakeTaskSpec()],
        team_recommendation=[
            _FakeAgentSpec(name="events-coordinator", role="Events"),
            _FakeAgentSpec(name="vendor-comms", role="Comms"),
        ],
    )

    with _patched_planner(fake):
        result = await planner_service.agent_plan_project(
            _ctx(),
            PlanProjectRequest(project_id=project_id, goal="Plan a thing carefully."),
        )

    gap_names = {g.spec_name for g in result.agent_gaps}
    assert gap_names == {"events-coordinator", "vendor-comms"}
    assert all(g.recommended_role for g in result.agent_gaps), (
        "recommended_role should not be empty when the planner provided one"
    )


async def test_plan_project_skips_prd_write_when_empty(recording_bus) -> None:
    """Empty PRD content → no PRD file id (goal.md + plan.json still land)."""

    project_id = await _make_project()
    fake = _FakePlannerResult(prd_content="", tasks=[_FakeTaskSpec()])

    with _patched_planner(fake):
        result = await planner_service.agent_plan_project(
            _ctx(),
            PlanProjectRequest(project_id=project_id, goal="A goal long enough."),
        )

    assert result.prd_file_id is None
    assert result.goal_file_id is not None
    assert result.plan_file_id is not None

    # Two file writes (goal + plan.json) → two FileReady events.
    file_ready = [e for e in recording_bus.events if isinstance(e, FileReady)]
    assert len(file_ready) == 2


# ---------------------------------------------------------------------------
# Tenant + validation guards
# ---------------------------------------------------------------------------


async def test_plan_project_requires_workspace() -> None:
    with pytest.raises(ValidationError):
        await planner_service.agent_plan_project(
            _ctx(workspace_id=None),
            PlanProjectRequest(project_id="x", goal="a long enough goal for validation"),
        )


async def test_plan_project_404s_unknown_project() -> None:
    fake = _FakePlannerResult()
    with _patched_planner(fake):
        with pytest.raises(NotFound):
            await planner_service.agent_plan_project(
                _ctx(),
                PlanProjectRequest(
                    project_id="000000000000000000000000",
                    goal="a long enough goal for validation",
                ),
            )


async def test_plan_project_404s_other_workspace_project() -> None:
    project_id = await _make_project()
    fake = _FakePlannerResult()
    with _patched_planner(fake):
        with pytest.raises(NotFound):
            await planner_service.agent_plan_project(
                _ctx(workspace_id="w2"),
                PlanProjectRequest(
                    project_id=project_id,
                    goal="a long enough goal for validation",
                ),
            )


# ---------------------------------------------------------------------------
# Failure surface
# ---------------------------------------------------------------------------


async def test_plan_project_wraps_oss_planner_errors_as_validation_error() -> None:
    project_id = await _make_project()
    fake = _FakePlannerResult()
    with _patched_planner(fake, raises=RuntimeError("LLM key missing")):
        with pytest.raises(ValidationError):
            await planner_service.agent_plan_project(
                _ctx(),
                PlanProjectRequest(project_id=project_id, goal="a long enough goal for validation"),
            )


# ---------------------------------------------------------------------------
# get_plan_for_project
# ---------------------------------------------------------------------------


async def test_get_plan_returns_none_when_no_prd_yet() -> None:
    project_id = await _make_project()
    out = await planner_service.get_plan_for_project(_ctx(), project_id)
    assert out is None


async def test_get_plan_returns_summary_after_run() -> None:
    project_id = await _make_project()
    fake = _FakePlannerResult(
        prd_content="# PRD",
        tasks=[_FakeTaskSpec(key="a", title="A"), _FakeTaskSpec(key="b", title="B")],
    )
    with _patched_planner(fake):
        first = await planner_service.agent_plan_project(
            _ctx(),
            PlanProjectRequest(project_id=project_id, goal="a long enough goal for validation"),
        )

    summary = await planner_service.get_plan_for_project(_ctx(), project_id)
    assert summary is not None
    assert summary.project_id == project_id
    assert summary.prd_file_id == first.prd_file_id
    assert set(summary.task_ids) == set(first.task_ids)
