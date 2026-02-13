# Tests for Deep Work Planner module.
# Created: 2026-02-12
#
# Tests cover:
#   - Prompt template placeholders
#   - JSON parsing (valid, code-fenced, invalid)
#   - PlannerResult construction
#   - ensure_profile (mocked manager)
#   - _broadcast_phase resilience

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketclaw.deep_work.models import AgentSpec, PlannerResult, TaskSpec
from pocketclaw.deep_work.planner import PlannerAgent
from pocketclaw.deep_work.prompts import (
    PRD_PROMPT,
    RESEARCH_PROMPT,
    TASK_BREAKDOWN_PROMPT,
    TEAM_ASSEMBLY_PROMPT,
)

# ============================================================================
# Prompt template tests
# ============================================================================


class TestPromptTemplates:
    """Verify prompt templates contain expected placeholders."""

    def test_research_prompt_has_placeholder(self):
        assert "{project_description}" in RESEARCH_PROMPT

    def test_prd_prompt_has_placeholders(self):
        assert "{project_description}" in PRD_PROMPT
        assert "{research_notes}" in PRD_PROMPT

    def test_task_breakdown_prompt_has_placeholders(self):
        assert "{project_description}" in TASK_BREAKDOWN_PROMPT
        assert "{prd_content}" in TASK_BREAKDOWN_PROMPT
        assert "{research_notes}" in TASK_BREAKDOWN_PROMPT

    def test_team_assembly_prompt_has_placeholder(self):
        assert "{tasks_json}" in TEAM_ASSEMBLY_PROMPT

    def test_research_prompt_can_be_formatted(self):
        result = RESEARCH_PROMPT.format(project_description="Build a TODO app")
        assert "Build a TODO app" in result
        assert "{project_description}" not in result

    def test_prd_prompt_can_be_formatted(self):
        result = PRD_PROMPT.format(
            project_description="Build a TODO app",
            research_notes="Some research",
        )
        assert "Build a TODO app" in result
        assert "Some research" in result

    def test_task_breakdown_prompt_can_be_formatted(self):
        result = TASK_BREAKDOWN_PROMPT.format(
            project_description="Build a TODO app",
            prd_content="PRD content here",
            research_notes="Research here",
        )
        assert "Build a TODO app" in result
        assert "PRD content here" in result

    def test_team_assembly_prompt_can_be_formatted(self):
        result = TEAM_ASSEMBLY_PROMPT.format(tasks_json='[{"key": "t1"}]')
        assert '{"key": "t1"}' in result


# ============================================================================
# JSON parsing tests
# ============================================================================


VALID_TASKS_JSON = json.dumps(
    [
        {
            "key": "t1",
            "title": "Set up project",
            "description": "Initialize the repo with boilerplate",
            "task_type": "agent",
            "priority": "high",
            "tags": ["setup"],
            "estimated_minutes": 15,
            "required_specialties": ["devops"],
            "blocked_by_keys": [],
        },
        {
            "key": "t2",
            "title": "Review setup",
            "description": "Check the project structure",
            "task_type": "review",
            "priority": "medium",
            "tags": ["review"],
            "estimated_minutes": 10,
            "required_specialties": [],
            "blocked_by_keys": ["t1"],
        },
    ]
)

VALID_TEAM_JSON = json.dumps(
    [
        {
            "name": "backend-dev",
            "role": "Backend Developer",
            "description": "Builds API endpoints and business logic",
            "specialties": ["python", "fastapi"],
            "backend": "claude_agent_sdk",
        },
        {
            "name": "qa-engineer",
            "role": "QA Engineer",
            "description": "Writes and runs tests",
            "specialties": ["testing", "pytest"],
            "backend": "claude_agent_sdk",
        },
    ]
)


class TestParseTasksPlain:
    """Test _parse_tasks with plain (non-fenced) JSON."""

    def setup_method(self):
        manager = MagicMock()
        self.planner = PlannerAgent(manager)

    def test_valid_json(self):
        tasks = self.planner._parse_tasks(VALID_TASKS_JSON)
        assert len(tasks) == 2
        assert isinstance(tasks[0], TaskSpec)
        assert tasks[0].key == "t1"
        assert tasks[0].title == "Set up project"
        assert tasks[0].task_type == "agent"
        assert tasks[0].priority == "high"
        assert tasks[0].estimated_minutes == 15
        assert tasks[0].required_specialties == ["devops"]

    def test_second_task_has_dependency(self):
        tasks = self.planner._parse_tasks(VALID_TASKS_JSON)
        assert tasks[1].key == "t2"
        assert tasks[1].blocked_by_keys == ["t1"]
        assert tasks[1].task_type == "review"


class TestParseTasksFenced:
    """Test _parse_tasks with markdown code-fenced JSON."""

    def setup_method(self):
        manager = MagicMock()
        self.planner = PlannerAgent(manager)

    def test_json_code_fence(self):
        fenced = f"```json\n{VALID_TASKS_JSON}\n```"
        tasks = self.planner._parse_tasks(fenced)
        assert len(tasks) == 2
        assert tasks[0].key == "t1"

    def test_plain_code_fence(self):
        fenced = f"```\n{VALID_TASKS_JSON}\n```"
        tasks = self.planner._parse_tasks(fenced)
        assert len(tasks) == 2

    def test_fence_with_surrounding_text(self):
        wrapped = f"Here is the breakdown:\n```json\n{VALID_TASKS_JSON}\n```\nDone!"
        tasks = self.planner._parse_tasks(wrapped)
        assert len(tasks) == 2


class TestParseTasksInvalid:
    """Test _parse_tasks with invalid input."""

    def setup_method(self):
        manager = MagicMock()
        self.planner = PlannerAgent(manager)

    def test_invalid_json(self):
        tasks = self.planner._parse_tasks("this is not json")
        assert tasks == []

    def test_empty_string(self):
        tasks = self.planner._parse_tasks("")
        assert tasks == []

    def test_json_object_not_list(self):
        tasks = self.planner._parse_tasks('{"key": "t1"}')
        assert tasks == []

    def test_json_with_non_dict_items(self):
        tasks = self.planner._parse_tasks('[1, 2, "string"]')
        assert tasks == []


class TestParseTeam:
    """Test _parse_team with various inputs."""

    def setup_method(self):
        manager = MagicMock()
        self.planner = PlannerAgent(manager)

    def test_valid_json(self):
        team = self.planner._parse_team(VALID_TEAM_JSON)
        assert len(team) == 2
        assert isinstance(team[0], AgentSpec)
        assert team[0].name == "backend-dev"
        assert team[0].role == "Backend Developer"
        assert team[0].specialties == ["python", "fastapi"]
        assert team[0].backend == "claude_agent_sdk"

    def test_fenced_json(self):
        fenced = f"```json\n{VALID_TEAM_JSON}\n```"
        team = self.planner._parse_team(fenced)
        assert len(team) == 2

    def test_invalid_json(self):
        team = self.planner._parse_team("not json")
        assert team == []

    def test_non_list_json(self):
        team = self.planner._parse_team('{"name": "dev"}')
        assert team == []


# ============================================================================
# PlannerResult construction tests
# ============================================================================


class TestPlannerResult:
    """Test constructing PlannerResult from parsed data."""

    def test_full_construction(self):
        manager = MagicMock()
        planner = PlannerAgent(manager)

        tasks = planner._parse_tasks(VALID_TASKS_JSON)
        team = planner._parse_team(VALID_TEAM_JSON)

        human_tasks = [t for t in tasks if t.task_type == "human"]
        agent_tasks = [t for t in tasks if t.task_type != "human"]

        dep_graph = {}
        for t in tasks:
            if t.blocked_by_keys:
                dep_graph[t.key] = list(t.blocked_by_keys)

        total_minutes = sum(t.estimated_minutes for t in tasks)

        result = PlannerResult(
            project_id="proj-123",
            prd_content="# PRD\nSome content",
            tasks=agent_tasks,
            team_recommendation=team,
            human_tasks=human_tasks,
            dependency_graph=dep_graph,
            estimated_total_minutes=total_minutes,
            research_notes="Some research notes",
        )

        assert result.project_id == "proj-123"
        assert result.prd_content == "# PRD\nSome content"
        assert len(result.tasks) == 2  # no human tasks in test data
        assert len(result.team_recommendation) == 2
        assert len(result.human_tasks) == 0
        assert result.dependency_graph == {"t2": ["t1"]}
        assert result.estimated_total_minutes == 25
        assert result.research_notes == "Some research notes"

    def test_result_to_dict(self):
        result = PlannerResult(
            project_id="proj-1",
            prd_content="PRD",
            tasks=[TaskSpec(key="t1", title="Task 1")],
            team_recommendation=[AgentSpec(name="dev", role="Developer")],
        )
        d = result.to_dict()
        assert d["project_id"] == "proj-1"
        assert len(d["tasks"]) == 1
        assert d["tasks"][0]["key"] == "t1"
        assert len(d["team_recommendation"]) == 1
        assert d["team_recommendation"][0]["name"] == "dev"


# ============================================================================
# ensure_profile tests (mocked manager)
# ============================================================================


class TestEnsureProfile:
    """Test ensure_profile with mocked MissionControlManager."""

    @pytest.mark.asyncio
    async def test_returns_existing_profile(self):
        manager = AsyncMock()
        existing_profile = MagicMock()
        existing_profile.name = "deep-work-planner"
        manager.get_agent_by_name = AsyncMock(return_value=existing_profile)

        planner = PlannerAgent(manager)
        profile = await planner.ensure_profile()

        assert profile is existing_profile
        manager.get_agent_by_name.assert_called_once_with("deep-work-planner")
        manager.create_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_new_profile(self):
        manager = AsyncMock()
        manager.get_agent_by_name = AsyncMock(return_value=None)
        new_profile = MagicMock()
        new_profile.name = "deep-work-planner"
        manager.create_agent = AsyncMock(return_value=new_profile)

        planner = PlannerAgent(manager)
        profile = await planner.ensure_profile()

        assert profile is new_profile
        manager.get_agent_by_name.assert_called_once_with("deep-work-planner")
        manager.create_agent.assert_called_once_with(
            name="deep-work-planner",
            role="Project Planner & Architect",
            description=(
                "Researches domains, generates PRDs, breaks projects "
                "into executable tasks, and recommends team composition"
            ),
            specialties=["planning", "research", "architecture", "task-decomposition"],
            backend="claude_agent_sdk",
        )


# ============================================================================
# _broadcast_phase resilience tests
# ============================================================================


class TestBroadcastPhase:
    """Test _broadcast_phase doesn't crash when bus is unavailable."""

    def test_no_crash_when_bus_unavailable(self):
        manager = MagicMock()
        planner = PlannerAgent(manager)
        # Should not raise even if bus module is not fully initialized
        with patch(
            "pocketclaw.bus.get_message_bus",
            side_effect=RuntimeError("no bus"),
        ):
            planner._broadcast_phase("proj-1", "research")

    def test_no_crash_with_no_event_loop(self):
        manager = MagicMock()
        planner = PlannerAgent(manager)
        # Should not raise even without a running event loop
        planner._broadcast_phase("proj-1", "prd")

    @pytest.mark.asyncio
    async def test_publishes_event_when_bus_available(self):
        manager = MagicMock()
        planner = PlannerAgent(manager)

        mock_bus = MagicMock()
        mock_bus.publish_system = AsyncMock()

        with patch(
            "pocketclaw.bus.get_message_bus",
            return_value=mock_bus,
        ):
            planner._broadcast_phase("proj-1", "tasks")

        # Give the fire-and-forget task a chance to run
        import asyncio

        await asyncio.sleep(0.05)


# ============================================================================
# Full plan() flow test (mocked _run_prompt)
# ============================================================================


class TestPlanFlow:
    """Test the full plan() flow with mocked _run_prompt."""

    @pytest.mark.asyncio
    async def test_plan_returns_planner_result(self):
        manager = AsyncMock()
        planner = PlannerAgent(manager)

        # Mock _run_prompt to return canned responses for each phase
        call_count = 0

        async def mock_run_prompt(prompt: str, router=None) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Research phase
                return "Domain research notes here"
            elif call_count == 2:
                # PRD phase
                return "## Problem Statement\nBuild a thing"
            elif call_count == 3:
                # Task breakdown phase
                return VALID_TASKS_JSON
            elif call_count == 4:
                # Team assembly phase
                return VALID_TEAM_JSON
            return ""

        planner._run_prompt = mock_run_prompt

        result = await planner.plan("Build a TODO app", project_id="proj-1")

        assert isinstance(result, PlannerResult)
        assert result.project_id == "proj-1"
        assert result.research_notes == "Domain research notes here"
        assert "Problem Statement" in result.prd_content
        assert len(result.tasks) == 2  # both are non-human
        assert len(result.team_recommendation) == 2
        assert result.estimated_total_minutes == 25
        assert result.dependency_graph == {"t2": ["t1"]}

    @pytest.mark.asyncio
    async def test_plan_with_human_tasks(self):
        manager = AsyncMock()
        planner = PlannerAgent(manager)

        tasks_with_human = json.dumps(
            [
                {
                    "key": "t1",
                    "title": "Decide feature scope",
                    "description": "Human decision needed",
                    "task_type": "human",
                    "priority": "high",
                    "tags": [],
                    "estimated_minutes": 60,
                    "required_specialties": [],
                    "blocked_by_keys": [],
                },
                {
                    "key": "t2",
                    "title": "Implement feature",
                    "description": "Build the thing",
                    "task_type": "agent",
                    "priority": "medium",
                    "tags": ["code"],
                    "estimated_minutes": 45,
                    "required_specialties": ["python"],
                    "blocked_by_keys": ["t1"],
                },
            ]
        )

        call_count = 0

        async def mock_run_prompt(prompt: str, router=None) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "Research"
            elif call_count == 2:
                return "PRD"
            elif call_count == 3:
                return tasks_with_human
            elif call_count == 4:
                return VALID_TEAM_JSON
            return ""

        planner._run_prompt = mock_run_prompt

        result = await planner.plan("Build something", project_id="proj-2")

        assert len(result.human_tasks) == 1
        assert result.human_tasks[0].key == "t1"
        assert result.human_tasks[0].task_type == "human"
        assert len(result.tasks) == 1  # only agent tasks
        assert result.tasks[0].key == "t2"
        assert result.estimated_total_minutes == 105


# ============================================================================
# _strip_code_fences edge cases
# ============================================================================


class TestStripCodeFences:
    """Test the static _strip_code_fences method."""

    def test_no_fences(self):
        assert PlannerAgent._strip_code_fences('  [{"key": "t1"}]  ') == '[{"key": "t1"}]'

    def test_json_fence(self):
        text = '```json\n[{"key": "t1"}]\n```'
        assert PlannerAgent._strip_code_fences(text) == '[{"key": "t1"}]'

    def test_plain_fence(self):
        text = '```\n[{"key": "t1"}]\n```'
        assert PlannerAgent._strip_code_fences(text) == '[{"key": "t1"}]'

    def test_surrounding_text_ignored(self):
        text = 'Here:\n```json\n{"a": 1}\n```\nEnd'
        assert PlannerAgent._strip_code_fences(text) == '{"a": 1}'

    def test_empty_string(self):
        assert PlannerAgent._strip_code_fences("") == ""
