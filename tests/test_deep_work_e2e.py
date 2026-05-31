# End-to-end tests for the Deep Work interactive intake mode (issue #1161).
# Created: 2026-05-21 (feat/deep-work-intake)
#
# These exercise the full intake → planning path against a real
# DeepWorkSession + real GoalParser + real PlannerAgent + real
# FileMissionControlStore. Only the LLM boundary is mocked — both the
# GoalParser and the PlannerAgent reach the model through ``_run_prompt``,
# which we replace with scripted responses.
#
# Coverage:
#   - A vague goal triggers the clarification loop.
#   - The clarification questions are surfaced to the answer provider.
#   - The collected answers are folded into the goal that planning sees.
#   - Planning runs to completion and produces a project + MC tasks.
#   - The resulting MC tasks carry the intake-captured ``success_criteria``.

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.deep_work.models import ProjectStatus
from pocketpaw.deep_work.planner import PlannerAgent
from pocketpaw.deep_work.session import DeepWorkSession
from pocketpaw.mission_control.manager import (
    MissionControlManager,
    reset_mission_control_manager,
)
from pocketpaw.mission_control.store import (
    FileMissionControlStore,
    reset_mission_control_store,
)

# ---------------------------------------------------------------------------
# Scripted LLM responses
# ---------------------------------------------------------------------------

# Goal parser, first call: a vague goal — two clarifications needed.
GOAL_PARSE_VAGUE = json.dumps(
    {
        "goal": "Chase down overdue invoices",
        "domain": "business",
        "complexity": "M",
        "estimated_phases": 2,
        "clarifications_needed": [
            "How many days overdue before an invoice counts?",
            "Which channel should reminders go out on?",
        ],
        "suggested_research_depth": "quick",
        "confidence": 0.5,
    }
)

# Goal parser, second call (after answers folded in): well-formed now.
GOAL_PARSE_CLEAR = json.dumps(
    {
        "goal": "Email reminders for invoices 30+ days overdue",
        "domain": "business",
        "complexity": "M",
        "estimated_phases": 2,
        "clarifications_needed": [],
        "suggested_research_depth": "quick",
        "confidence": 0.9,
    }
)

# Planner task-breakdown response — two tasks, each with success_criteria
# and preconditions (the fields issue #1161 adds to TaskSpec).
PLANNER_TASKS = json.dumps(
    [
        {
            "key": "t1",
            "title": "Pull the list of overdue invoices",
            "description": "Query accounting for invoices 30+ days overdue",
            "task_type": "agent",
            "priority": "high",
            "tags": ["finance"],
            "estimated_minutes": 20,
            "required_specialties": ["data"],
            "blocked_by_keys": [],
            "success_criteria": [
                "A list of invoices each 30+ days past due is produced",
                "Every row has an amount and a customer email",
            ],
            "preconditions": ["Skip if the accounting connector is not configured"],
        },
        {
            "key": "t2",
            "title": "Send reminder emails",
            "description": "Email each overdue customer a payment reminder",
            "task_type": "agent",
            "priority": "high",
            "tags": ["finance"],
            "estimated_minutes": 30,
            "required_specialties": ["email"],
            "blocked_by_keys": ["t1"],
            "success_criteria": ["One reminder email is sent per overdue invoice"],
            "preconditions": ["Do not email customers flagged do-not-contact"],
        },
    ]
)

# Planner team-assembly response.
PLANNER_TEAM = json.dumps(
    [
        {
            "name": "finance-bot",
            "role": "Finance Assistant",
            "description": "Handles invoice chasing",
            "specialties": ["data", "email"],
            "backend": "claude_agent_sdk",
        }
    ]
)

# A short PRD / research blob — anything non-empty works.
PRD_TEXT = "# Overdue Invoice Chaser\n\n## Problem Statement\nChase overdue invoices."
RESEARCH_TEXT = "Domain overview: invoice collection."


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_store_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def manager(temp_store_path):
    reset_mission_control_store()
    reset_mission_control_manager()
    return MissionControlManager(FileMissionControlStore(temp_store_path))


@pytest.fixture
def mock_executor():
    executor = MagicMock()
    executor.is_task_running = MagicMock(return_value=False)
    executor.stop_task = AsyncMock(return_value=True)
    executor.execute_task_background = AsyncMock()
    return executor


@pytest.fixture
def mock_human_router():
    router = MagicMock()
    router.notify_human_task = AsyncMock()
    router.notify_review_task = AsyncMock()
    router.notify_plan_ready = AsyncMock()
    router.notify_project_completed = AsyncMock()
    return router


@pytest.fixture
def planner(manager):
    """A real PlannerAgent with its LLM boundary (_run_prompt) scripted.

    The planner runs research → PRD → task breakdown → team assembly. We
    return the right scripted blob based on which phase the prompt is for,
    so a real PlannerResult (with TaskSpecs) comes out the other end.
    """
    agent = PlannerAgent(manager)

    async def scripted_run_prompt(prompt: str, router=None) -> str:
        # Cheap phase detection off the prompt's distinguishing text.
        if "JSON array of task objects" in prompt or "project architect" in prompt:
            return PLANNER_TASKS
        if "team architect" in prompt:
            return PLANNER_TEAM
        if "product manager" in prompt:
            return PRD_TEXT
        return RESEARCH_TEXT

    agent._run_prompt = scripted_run_prompt
    return agent


@pytest.fixture
def session(manager, mock_executor, planner, mock_human_router):
    return DeepWorkSession(
        manager=manager,
        executor=mock_executor,
        planner=planner,
        human_router=mock_human_router,
    )


def _patched_goal_parser():
    """Patch GoalParser._run_prompt so both the initial parse (vague) and
    the post-fold re-parse (clear) get scripted responses in order."""
    calls = {"n": 0}
    responses = [GOAL_PARSE_VAGUE, GOAL_PARSE_CLEAR]

    async def scripted(self, prompt: str) -> str:  # noqa: ARG001
        idx = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[idx]

    return patch.object(
        __import__("pocketpaw.deep_work.goal_parser", fromlist=["GoalParser"]).GoalParser,
        "_run_prompt",
        scripted,
    ), calls


# ---------------------------------------------------------------------------
# End-to-end intake tests
# ---------------------------------------------------------------------------


class TestIntakeEndToEnd:
    """The full vague-goal → clarify → fold → plan path."""

    @pytest.mark.asyncio
    async def test_vague_goal_runs_intake_then_plans(self, session, manager):
        """A vague goal: clarifications are asked, answers folded, planning
        runs to AWAITING_APPROVAL, and the resulting tasks carry
        success_criteria."""
        parser_patch, parser_calls = _patched_goal_parser()

        asked: list[str] = []

        async def answer_provider(question: str) -> str:
            asked.append(question)
            if "overdue" in question:
                return "30 days"
            return "email"

        with parser_patch:
            project = await session.start_with_intake(
                "Chase down overdue invoices", answer_provider
            )

        # --- intake happened ---
        # Both clarification questions were surfaced to the human.
        assert len(asked) == 2
        # The goal parser ran twice: initial vague parse + re-parse of the
        # enriched goal.
        assert parser_calls["n"] == 2

        # --- the answers were folded into the planned goal ---
        assert project.metadata.get("intake") is not None
        intake = project.metadata["intake"]
        assert intake["clarified"] is True
        assert "30 days" in intake["enriched_goal"]
        assert "email" in intake["enriched_goal"]
        # The project description is the enriched goal.
        assert "Chase down overdue invoices" in project.description
        assert "30 days" in project.description

        # --- planning ran to completion ---
        assert project.status == ProjectStatus.AWAITING_APPROVAL

        # --- resulting MC tasks carry success_criteria ---
        tasks = await manager.get_project_tasks(project.id)
        assert len(tasks) == 2
        for task in tasks:
            criteria = task.metadata.get("success_criteria")
            assert criteria, f"task {task.title!r} is missing success_criteria"
            assert isinstance(criteria, list)
            assert all(isinstance(c, str) and c for c in criteria)

        # The first task's preconditions also rode through.
        t1 = next(t for t in tasks if "Pull the list" in t.title)
        assert t1.metadata.get("preconditions") == [
            "Skip if the accounting connector is not configured"
        ]

    @pytest.mark.asyncio
    async def test_success_criteria_survive_store_round_trip(self, session, manager):
        """success_criteria persisted on a task must reload intact — this is
        the field the outcome-verification sibling (#1162) depends on."""
        parser_patch, _ = _patched_goal_parser()

        async def answer_provider(question: str) -> str:
            return "a concrete answer"

        with parser_patch:
            project = await session.start_with_intake(
                "Chase down overdue invoices", answer_provider
            )

        # Reload each task fresh from the store (not the in-memory copy).
        tasks = await manager.get_project_tasks(project.id)
        for task in tasks:
            reloaded = await manager.get_task(task.id)
            assert reloaded is not None
            assert reloaded.metadata.get("success_criteria") == task.metadata.get(
                "success_criteria"
            )

    @pytest.mark.asyncio
    async def test_intake_transcript_recorded_in_order(self, session, manager):
        """The Q&A transcript on the project preserves question order."""
        parser_patch, _ = _patched_goal_parser()

        async def answer_provider(question: str) -> str:
            return "30 days" if "overdue" in question else "email"

        with parser_patch:
            project = await session.start_with_intake(
                "Chase down overdue invoices", answer_provider
            )

        transcript = project.metadata["intake"]["transcript"]
        assert len(transcript) == 2
        assert "overdue" in transcript[0]["question"]
        assert transcript[0]["answer"] == "30 days"
        assert transcript[1]["answer"] == "email"


class TestIntakeOneShotPath:
    """A well-formed goal must skip intake and behave like start()."""

    @pytest.mark.asyncio
    async def test_well_formed_goal_skips_clarification(self, session, manager):
        """When the goal parser surfaces no clarifications, the answer
        provider is never called and planning runs straight through."""
        calls = {"n": 0}

        async def scripted(self, prompt: str) -> str:  # noqa: ARG001
            calls["n"] += 1
            # Always well-formed — no clarifications_needed.
            return GOAL_PARSE_CLEAR

        asked: list[str] = []

        async def answer_provider(question: str) -> str:
            asked.append(question)
            return "unused"

        from pocketpaw.deep_work.goal_parser import GoalParser

        with patch.object(GoalParser, "_run_prompt", scripted):
            project = await session.start_with_intake(
                "Email reminders for invoices 30+ days overdue", answer_provider
            )

        # The answer provider was never invoked — intake was a no-op.
        assert asked == []
        # Parser ran exactly once (the initial parse; no re-parse needed).
        assert calls["n"] == 1
        # Planning still completed.
        assert project.status == ProjectStatus.AWAITING_APPROVAL
        # intake metadata records that nothing was clarified.
        assert project.metadata["intake"]["clarified"] is False

        tasks = await manager.get_project_tasks(project.id)
        assert len(tasks) == 2
