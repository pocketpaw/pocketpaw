# End-to-end tests for the Deep Work interactive intake mode (issue #1161).
# Created: 2026-05-21 (feat/deep-work-intake)
# Updated: 2026-06-23 (feat/svl-1-verify-stamp) — added
#   TestVerifyOnCompletion: drives the MC executor to completion with the
#   Self-Verifying-Loop flag on and asserts the OutcomeVerdict stamped on
#   ``task.metadata["verify_verdict"]`` matches a direct ``verify_outcome``
#   call, plus a flag-off case proving the verdict is absent and behaviour is
#   unchanged.
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
#   - With the verify loop ON, a completing task is stamped with its verdict;
#     with it OFF, the verdict is absent (SVL-1).

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


# ---------------------------------------------------------------------------
# Self-Verifying Loop — verify-on-completion stamp (SVL-1)
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

import pocketpaw.mission_control.store as store_module  # noqa: E402
from pocketpaw.agents.protocol import AgentEvent  # noqa: E402
from pocketpaw.config import get_settings  # noqa: E402
from pocketpaw.instinct.verification import verify_outcome  # noqa: E402
from pocketpaw.mission_control import TaskPriority, TaskStatus  # noqa: E402
from pocketpaw.mission_control.executor import (  # noqa: E402
    get_mc_task_executor,
    reset_mc_task_executor,
)

# Output the mock agent "produces" — text that satisfies the criteria below.
# The deterministic verifier passes a criterion when every content word of it
# appears in the result, so this output is crafted to MEET both criteria.
_SUCCESS_OUTPUT = (
    "Produced a list of overdue invoices, each 30+ days past due. "
    "Every row carries an amount and a customer email address."
)
_SUCCESS_CRITERIA = [
    "A list of invoices each 30+ days past due is produced",
    "Every row has an amount and a customer email",
]


@pytest.fixture
def svl_singletons(temp_store_path):
    """Reset MC singletons and wire them all to a shared temp store.

    The executor reaches the store via the module singletons (not the
    intake ``manager`` fixture), so the verify-on-completion tests inject the
    store the same way ``test_mission_control_executor.py`` does.
    """
    reset_mission_control_store()
    reset_mission_control_manager()
    reset_mc_task_executor()

    test_store = FileMissionControlStore(temp_store_path)
    store_module._store_instance = test_store

    yield test_store

    reset_mission_control_store()
    reset_mission_control_manager()
    reset_mc_task_executor()


def _svl_mock_router():
    """A mock AgentRouter that yields the success output then 'done'."""

    async def mock_run(prompt):  # noqa: ARG001
        yield AgentEvent(type="message", content=_SUCCESS_OUTPUT)
        yield AgentEvent(type="done", content="")
        await asyncio.sleep(0)

    router = MagicMock()
    router.run = mock_run
    router.stop = AsyncMock()
    return router


def _settings_with_verify(enabled: bool):
    """A Settings copy with the verify-loop flag forced to ``enabled``."""
    return get_settings().model_copy(update={"deep_work_verify_loop_enabled": enabled})


class TestVerifyOnCompletion:
    """SVL-1: a completing task is stamped with its outcome verdict when the
    Self-Verifying-Loop flag is on, and left untouched when it is off."""

    async def _run_one_task(self, svl_singletons, *, verify_enabled: bool):
        """Create an agent + a task with success_criteria, run it to
        completion through the executor with the flag set, and return the
        reloaded task."""
        from pocketpaw.mission_control import get_mission_control_manager

        manager = get_mission_control_manager()
        executor = get_mc_task_executor()

        agent = await manager.create_agent(
            name="FinanceBot",
            role="Finance Assistant",
            description="Chases overdue invoices",
            backend="claude_agent_sdk",
        )
        task = await manager.create_task(
            title="Pull the list of overdue invoices",
            description="Query accounting for invoices 30+ days overdue",
            priority=TaskPriority.HIGH,
        )
        # Stamp the intake success_criteria the planner would have captured.
        task.metadata["success_criteria"] = list(_SUCCESS_CRITERIA)
        await manager.save_task(task)
        await manager.assign_task(task.id, [agent.id])

        mock_router = _svl_mock_router()
        with (
            patch(
                "pocketpaw.mission_control.executor.AgentRouter",
                return_value=mock_router,
            ),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=_settings_with_verify(verify_enabled),
            ),
            patch("pocketpaw.mission_control.executor.get_message_bus") as mock_bus,
        ):
            mock_bus.return_value.publish_system = AsyncMock()
            result = await executor.execute_task(task.id, agent.id)

        assert result["status"] == "completed"
        reloaded = await manager.get_task(task.id)
        assert reloaded is not None
        return reloaded

    @pytest.mark.asyncio
    async def test_flag_on_stamps_verdict_matching_direct_verify(self, svl_singletons):
        """With the flag enabled, a successfully-completing task whose criteria
        are met carries a ``verify_verdict`` whose status matches a direct
        ``verify_outcome`` call on the same output + criteria."""
        task = await self._run_one_task(svl_singletons, verify_enabled=True)

        # Task still completed normally — status is DONE, not changed by verify.
        assert task.status == TaskStatus.DONE

        verdict = task.metadata.get("verify_verdict")
        assert verdict is not None, "verify_verdict was not stamped with the flag on"

        # The stamped verdict matches an independent verify_outcome call.
        expected = verify_outcome(task.output, _SUCCESS_CRITERIA)
        assert verdict["status"] == expected.status.value
        # Criteria met → SOLVED.
        assert verdict["status"] == "solved"

    @pytest.mark.asyncio
    async def test_flag_off_leaves_no_verdict_and_unchanged_behaviour(self, svl_singletons):
        """With the flag off (default), no verdict is stamped and the task
        completes exactly as before — status DONE, output intact."""
        task = await self._run_one_task(svl_singletons, verify_enabled=False)

        assert task.status == TaskStatus.DONE
        assert task.output and "overdue invoices" in task.output
        assert "verify_verdict" not in task.metadata
