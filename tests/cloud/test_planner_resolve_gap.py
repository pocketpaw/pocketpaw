# Created: 2026-05-17 — pocketpaw#1118 P3. Tests for
#   ``planner.service.agent_resolve_gap`` — the agent-gap →
#   create-agent → reassign flow. Drives an end-to-end materialization
#   with a stubbed OSS planner so the test fixtures look like the
#   production code path; resolve_gap then exercises the
#   tenant-checked reassignment + gap pruning.
"""Service-level coverage for ``planner.service.agent_resolve_gap``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud._core.errors import NotFound
from ee.cloud._core.realtime.events import PlanGapResolved
from ee.cloud.agents import service as agents_service
from ee.cloud.agents.dto import CreateAgentRequest
from ee.cloud.planner import service as planner_service
from ee.cloud.planner.dto import PlanProjectRequest, ResolveGapRequest
from ee.cloud.projects import service as projects_service
from ee.cloud.projects.dto import CreateProjectRequest
from ee.cloud.tasks import service as tasks_service
from ee.cloud.tasks.dto import ListTasksRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(*, user_id: str = "u-creator", workspace_id: str = "w1") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="r-resolve",
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


class _FakeAgentSpec:
    def __init__(self, *, name: str, role: str = "", specialties: list[str] | None = None) -> None:
        self.name = name
        self.role = role
        self.specialties = specialties or []
        self.backend = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "role": self.role}


class _FakePlannerResult:
    def __init__(
        self,
        *,
        prd_content: str = "# PRD",
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


def _patched_planner(result: _FakePlannerResult):
    async def _fake_plan(self, project_description, project_id="", research_depth="standard"):  # noqa: ARG001
        result.project_id = project_id
        return result

    return patch("pocketpaw.deep_work.planner.PlannerAgent.plan", new=_fake_plan)


@pytest.fixture(autouse=True)
def _isolated_upload_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin Path.home() into tmp_path so planner file writes stay hermetic."""

    monkeypatch.setattr(Path, "home", lambda: tmp_path)


async def _make_project(workspace_id: str = "w1", lead_id: str = "u-lead") -> str:
    proj = await projects_service.agent_create(
        _ctx(workspace_id=workspace_id),
        CreateProjectRequest(name="Crestline", lead_id=lead_id),
    )
    return proj.id


async def _create_agent(
    workspace_id: str = "w1",
    *,
    name: str,
    slug: str | None = None,
) -> str:
    agent = await agents_service.create(
        _ctx(workspace_id=workspace_id),
        workspace_id,
        CreateAgentRequest(name=name, slug=slug or name.lower().replace(" ", "-")),
    )
    return agent.id


async def _plan_with_unmatched_spec(
    *,
    project_id: str,
    spec_name: str = "events-coordinator",
    extra_specs: list[_FakeAgentSpec] | None = None,
) -> str:
    """Run a plan that creates 3 tasks wanting ``spec_name``. All three
    fall back to the project lead (human) because no cloud Agent exists.
    Returns the plan_session_id.
    """

    fake = _FakePlannerResult(
        tasks=[
            _FakeTaskSpec(
                key=f"t{i}",
                title=f"Task {i}",
                required_specialties=["events"],
            )
            for i in range(3)
        ],
        team_recommendation=[
            _FakeAgentSpec(name=spec_name, role="Events Lead", specialties=["events"]),
            *(extra_specs or []),
        ],
    )
    with _patched_planner(fake):
        result = await planner_service.agent_plan_project(
            _ctx(),
            PlanProjectRequest(project_id=project_id, goal="Plan the wedding for May 23."),
        )
    return result.plan_session_id


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_resolve_gap_reassigns_human_fallback_tasks(recording_bus) -> None:
    project_id = await _make_project()
    plan_session_id = await _plan_with_unmatched_spec(project_id=project_id)

    # Confirm baseline: three tasks landed as human fallback with the
    # planner-wanted spec name on assignee.name.
    rows_before = await tasks_service.agent_list_tasks(
        _ctx(), ListTasksRequest(project_id=project_id, limit=100)
    )
    assert len(rows_before) == 3
    assert all(r.assignee.kind == "human" for r in rows_before)
    assert all(r.assignee.name == "events-coordinator" for r in rows_before)

    # Operator creates the agent the planner wanted. Frontend hits
    # ``POST /api/v1/agents`` directly; we simulate that here.
    new_agent_id = await _create_agent(name="events-coordinator")

    result = await planner_service.agent_resolve_gap(
        _ctx(),
        ResolveGapRequest(
            plan_session_id=plan_session_id,
            spec_name="events-coordinator",
            new_agent_id=new_agent_id,
        ),
    )

    assert len(result.reassigned_task_ids) == 3
    assert result.remaining_gaps == []
    assert result.new_agent_id == new_agent_id

    rows_after = await tasks_service.agent_list_tasks(
        _ctx(), ListTasksRequest(project_id=project_id, limit=100)
    )
    assert all(r.assignee.kind == "agent" for r in rows_after)
    assert all(r.assignee.id == new_agent_id for r in rows_after)
    assert all(r.assignee.name == "events-coordinator" for r in rows_after)

    # Event fan-out: at least one PlanGapResolved per resolve call.
    gap_events = [e for e in recording_bus.events if isinstance(e, PlanGapResolved)]
    assert len(gap_events) == 1
    payload = gap_events[0].data
    assert payload["spec_name"] == "events-coordinator"
    assert payload["new_agent_id"] == new_agent_id
    assert payload["reassigned_task_count"] == 3
    assert payload["remaining_gap_count"] == 0


async def test_resolve_gap_returns_remaining_gaps() -> None:
    project_id = await _make_project()
    plan_session_id = await _plan_with_unmatched_spec(
        project_id=project_id,
        extra_specs=[
            _FakeAgentSpec(name="vendor-comms", role="Comms", specialties=["comms"]),
        ],
    )

    new_agent_id = await _create_agent(name="events-coordinator")
    result = await planner_service.agent_resolve_gap(
        _ctx(),
        ResolveGapRequest(
            plan_session_id=plan_session_id,
            spec_name="events-coordinator",
            new_agent_id=new_agent_id,
        ),
    )

    remaining_names = {g.spec_name for g in result.remaining_gaps}
    assert remaining_names == {"vendor-comms"}


async def test_resolve_gap_404_for_unknown_session() -> None:
    new_agent_id = await _create_agent(name="some-agent")
    with pytest.raises(NotFound):
        await planner_service.agent_resolve_gap(
            _ctx(),
            ResolveGapRequest(
                plan_session_id="000000000000000000000000",
                spec_name="some-agent",
                new_agent_id=new_agent_id,
            ),
        )


async def test_resolve_gap_404_for_unknown_agent_id() -> None:
    project_id = await _make_project()
    plan_session_id = await _plan_with_unmatched_spec(project_id=project_id)

    with pytest.raises(NotFound):
        await planner_service.agent_resolve_gap(
            _ctx(),
            ResolveGapRequest(
                plan_session_id=plan_session_id,
                spec_name="events-coordinator",
                new_agent_id="000000000000000000000000",
            ),
        )


async def test_resolve_gap_tenant_isolation() -> None:
    project_id = await _make_project(workspace_id="w1")
    plan_session_id = await _plan_with_unmatched_spec(project_id=project_id)
    # Agent lives in another workspace; the resolve attempt from w2
    # against w1's session must 404 — same uniform code path the tasks
    # service uses to mitigate id enumeration.
    foreign_agent_id = await _create_agent(workspace_id="w2", name="events-coordinator")
    with pytest.raises(NotFound):
        await planner_service.agent_resolve_gap(
            _ctx(workspace_id="w2"),
            ResolveGapRequest(
                plan_session_id=plan_session_id,
                spec_name="events-coordinator",
                new_agent_id=foreign_agent_id,
            ),
        )
