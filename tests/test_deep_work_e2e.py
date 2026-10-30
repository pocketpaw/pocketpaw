# End-to-end tests for the Deep Work interactive intake mode (issue #1161).
# Created: 2026-05-21 (feat/deep-work-intake)
# Updated: 2026-07-10 (feat/verify-mode-shadow) — added TestVerifyShadowMode:
#   the three-position ``deep_work_verify_mode`` rollout switch. shadow: a
#   FAILING task still lands DONE with its deliverable saved, the verdict +
#   ``verify_would_have`` + ``verify_mode='shadow'`` stamped, and NO
#   ``verify_requeue_count`` / ``verify_feedback`` written; the judge shadow
#   keeps working under shadow mode. mode='enforce' alone (bool off) drives
#   the full requeue loop; the legacy bool alone still ENFORCES
#   (back-compat — it must never silently weaken to shadow); mode='off'
#   stays byte-for-byte inert. The pre-existing TestVerify* classes set only
#   the legacy bool, so their continued passing is the enforce==today proof.
# Updated: 2026-07-02 (feat/judge-shadow-1168) — added TestJudgeShadow (J-1,
#   #1168): with BOTH flags on, a deterministic FAIL + judge PASS still
#   requeues the task (the judge NEVER rescues a failing result), the judge
#   verdict is stamped observe-only on ``verify_judge_verdict`` and the
#   agree=False shadow log line fires; with the judge flag off the provider is
#   never constructed and no judge verdict is stamped; judge-on + loop-off
#   runs nothing; a judge exception never breaks task completion. The judge's
#   transport is always a FAKE — no real ``claude`` subprocess in tests.
# Updated: 2026-06-23 (feat/svl-3-no-progress) — added TestVerifyNoProgress:
#   drives the MC executor's verify loop with a budget > 1 and proves the SVL-3
#   no-progress / oscillation guard. A task that returns the SAME failing output
#   (same unmet criteria) on consecutive attempts escalates to BLOCKED with
#   ``verify_escalation_reason == "no_progress"`` BEFORE the requeue budget is
#   spent, and the escalation activity/broadcast reflects the no_progress cause.
#   A task whose unmet set strictly shrinks (2 → 1 → solved) keeps requeuing and
#   is NOT escalated early — it converges to DONE.
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


# ---------------------------------------------------------------------------
# SVL-2: requeue loop with diagnostic feedback + budget
# ---------------------------------------------------------------------------

# Output that DOES NOT satisfy either criterion in _SUCCESS_CRITERIA — the
# deterministic verifier needs every content token of a criterion to appear in
# the result, so a result about something unrelated yields NOT_SOLVED.
_FAILING_OUTPUT = "I looked into the request but could not find the data needed."


def _svl_failing_router():
    """A mock AgentRouter whose output FAILS the success criteria every time."""

    async def mock_run(prompt):  # noqa: ARG001
        yield AgentEvent(type="message", content=_FAILING_OUTPUT)
        yield AgentEvent(type="done", content="")
        await asyncio.sleep(0)

    router = MagicMock()
    router.run = mock_run
    router.stop = AsyncMock()
    return router


# --- Budget-exhaustion scenario (three criteria, shrinking-but-never-solved) --
#
# SVL-3 escalates EARLY when an attempt makes no progress (same or non-shrinking
# unmet set). So to exercise SVL-2's BUDGET-exhaustion path we need a task that
# keeps making genuine progress every requeue yet never fully solves before the
# budget runs out. With three criteria the unmet set can shrink 3 → 2 → 1 across
# a budget of 2 and still be failing at the escalation point — progress on every
# pass (no-progress guard never fires), budget exhausted at the end.
_BUDGET_CRITERIA = [
    "A list of invoices each 30+ days past due is produced",
    "Every row has an amount and a customer email",
    "A summary total of the outstanding balance is included",
]
# outputs[i] meets i criteria: 0 → 1 → 2 (never all 3). Each leaves a strictly
# smaller unmet set than the last: 3 unmet → 2 unmet → 1 unmet.
_BUDGET_OUTPUTS = [
    "I could not retrieve the requested information at all.",
    "A summary total of the outstanding balance is included in my notes, nothing else done.",
    (
        "Each row has an amount and a customer email. "
        "A summary total of the outstanding balance is included."
    ),
]


def _svl_shrinking_failing_router():
    """A mock AgentRouter that meets one MORE criterion each call but never all
    of them — the unmet set strictly shrinks every requeue yet the result keeps
    failing. Drives SVL-2's budget-exhaustion path WITHOUT tripping the SVL-3
    no-progress guard (which would escalate a stuck/non-shrinking task early)."""
    state = {"calls": 0}

    async def mock_run(prompt):  # noqa: ARG001
        idx = min(state["calls"], len(_BUDGET_OUTPUTS) - 1)
        state["calls"] += 1
        yield AgentEvent(type="message", content=_BUDGET_OUTPUTS[idx])
        yield AgentEvent(type="done", content="")
        await asyncio.sleep(0)

    router = MagicMock()
    router.run = mock_run
    router.stop = AsyncMock()
    return router


def _svl_converging_router(fail_times: int, *, captured_prompts: list[str] | None = None):
    """A mock AgentRouter that FAILS its criteria ``fail_times`` times, then
    succeeds — to prove the loop converges once the agent finally satisfies the
    criteria. Optionally records each prompt it was handed so a test can assert
    the rejection feedback was injected on the requeue."""
    state = {"calls": 0}

    async def mock_run(prompt):
        if captured_prompts is not None:
            captured_prompts.append(prompt)
        state["calls"] += 1
        content = _FAILING_OUTPUT if state["calls"] <= fail_times else _SUCCESS_OUTPUT
        yield AgentEvent(type="message", content=content)
        yield AgentEvent(type="done", content="")
        await asyncio.sleep(0)

    router = MagicMock()
    router.run = mock_run
    router.stop = AsyncMock()
    return router


def _settings_with_verify_budget(enabled: bool, max_requeues: int):
    """A Settings copy with the verify-loop flag and requeue budget forced."""
    return get_settings().model_copy(
        update={
            "deep_work_verify_loop_enabled": enabled,
            "deep_work_verify_max_requeues": max_requeues,
        }
    )


class TestVerifyRequeueLoop:
    """SVL-2: a completed task whose result does NOT meet its success_criteria
    is requeued with the specific unmet criteria fed back, bounded by
    ``deep_work_verify_max_requeues``, then escalated to BLOCKED."""

    async def _make_agent_and_task(self):
        from pocketpaw.mission_control import get_mission_control_manager

        manager = get_mission_control_manager()
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
        task.metadata["success_criteria"] = list(_SUCCESS_CRITERIA)
        await manager.save_task(task)
        await manager.assign_task(task.id, [agent.id])
        return manager, agent, task

    async def _make_budget_agent_and_task(self):
        """Same as ``_make_agent_and_task`` but with the THREE-criterion
        budget-exhaustion criteria — paired with ``_svl_shrinking_failing_router``
        so the unmet set shrinks every requeue (no SVL-3 early escalation) and
        the BUDGET is what finally trips the escalation."""
        from pocketpaw.mission_control import get_mission_control_manager

        manager = get_mission_control_manager()
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
        task.metadata["success_criteria"] = list(_BUDGET_CRITERIA)
        await manager.save_task(task)
        await manager.assign_task(task.id, [agent.id])
        return manager, agent, task

    async def _run_once(self, executor, task_id, agent_id, mock_router, settings):
        """Run a single execute_task pass with the verify settings patched.

        Mirrors exactly what the production re-dispatch
        (``execute_task_background`` → ``execute_task``) invokes, but driven
        synchronously so transitions can be asserted step by step."""
        with (
            patch(
                "pocketpaw.mission_control.executor.AgentRouter",
                return_value=mock_router,
            ),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=settings,
            ),
            patch("pocketpaw.mission_control.executor.get_message_bus") as mock_bus,
        ):
            mock_bus.return_value.publish_system = AsyncMock()
            return await executor.execute_task(task_id, agent_id)

    @pytest.mark.asyncio
    async def test_failing_task_requeues_then_escalates_to_blocked(self, svl_singletons):
        """A task whose output never meets its criteria is requeued at most
        ``deep_work_verify_max_requeues`` times (each requeue back to ASSIGNED),
        then lands BLOCKED with the escalation reason stamped. The verify
        counter is separate from the error retry_count."""

        manager, agent, task = await self._make_budget_agent_and_task()
        executor = get_mc_task_executor()
        max_requeues = 2
        settings = _settings_with_verify_budget(True, max_requeues)
        # Shrinking-but-never-solved output so the budget (not the SVL-3
        # no-progress guard) is what finally escalates the task.
        router = _svl_shrinking_failing_router()

        # First two passes requeue (status back to ASSIGNED), counter climbs.
        for expected_n in (1, 2):
            await self._run_once(executor, task.id, agent.id, router, settings)
            reloaded = await manager.get_task(task.id)
            assert reloaded.status == TaskStatus.ASSIGNED, (
                f"pass {expected_n} should requeue to ASSIGNED, got {reloaded.status}"
            )
            assert reloaded.metadata["verify_requeue_count"] == expected_n
            # Verify counter must NOT bleed into the error retry_count.
            assert reloaded.retry_count == 0
            assert "verify_escalation_reason" not in reloaded.metadata

        # Third pass: budget exhausted → BLOCKED + reason stamped.
        await self._run_once(executor, task.id, agent.id, router, settings)
        blocked = await manager.get_task(task.id)
        assert blocked.status == TaskStatus.BLOCKED
        assert blocked.metadata["verify_escalation_reason"] == "budget_exhausted"
        # Bounded: never exceeded the budget.
        assert blocked.metadata["verify_requeue_count"] == max_requeues
        # Cumulative feedback records every rejected attempt.
        feedback = blocked.metadata["verify_feedback"]
        assert len(feedback) == max_requeues
        # The error retry path was never touched.
        assert blocked.retry_count == 0

    @pytest.mark.asyncio
    async def test_escalation_does_not_mark_done_or_save_deliverable(self, svl_singletons):
        """The whole point of the loop is "don't mark failing work as done". A
        budget-exhausted escalation must NOT log a TASK_COMPLETED activity and
        must NOT save the rejected output as a deliverable — it must instead log
        a needs-review escalation activity."""
        from pocketpaw.mission_control import ActivityType

        manager, agent, task = await self._make_budget_agent_and_task()
        executor = get_mc_task_executor()
        max_requeues = 1  # exhaust quickly: one requeue, then escalate
        settings = _settings_with_verify_budget(True, max_requeues)
        # Shrinking-but-never-solved so it escalates on BUDGET, not no-progress.
        router = _svl_shrinking_failing_router()

        # Spy on the deliverable save across every pass — it must never fire for
        # this task, since the output never passes its criteria.
        with patch.object(executor, "_save_task_deliverable", new=AsyncMock()) as deliverable_spy:
            # Pass 1: requeue. Pass 2: budget (1) exhausted → escalate to BLOCKED.
            await self._run_once(executor, task.id, agent.id, router, settings)
            await self._run_once(executor, task.id, agent.id, router, settings)

        blocked = await manager.get_task(task.id)
        assert blocked.status == TaskStatus.BLOCKED
        assert blocked.metadata["verify_escalation_reason"] == "budget_exhausted"

        # The failing output was NEVER saved as a deliverable.
        deliverable_spy.assert_not_called()
        # And no DELIVERABLE document exists for the task.
        docs = await manager.get_task_documents(task.id)
        assert docs == [] or all(d.type.value != "deliverable" for d in docs)

        # No TASK_COMPLETED activity was logged for this task; an escalation
        # (TASK_UPDATED, "escalated ... needs review") was.
        feed = await manager.get_activity_feed(limit=100)
        task_activities = [a for a in feed if a.task_id == task.id]
        assert all(a.type != ActivityType.TASK_COMPLETED for a in task_activities), (
            "an escalated task must not log a TASK_COMPLETED activity"
        )
        escalations = [
            a
            for a in task_activities
            if a.type == ActivityType.TASK_UPDATED and "escalated" in a.message
        ]
        assert escalations, "expected a needs-review escalation activity"
        assert "needs review" in escalations[0].message

    @pytest.mark.asyncio
    async def test_requeue_prompt_carries_the_unmet_criteria(self, svl_singletons):
        """The rebuilt prompt for a requeued attempt names the SPECIFIC unmet
        criteria so the re-dispatched agent sees exactly what to fix."""

        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_budget(True, 2)

        captured: list[str] = []
        router = _svl_converging_router(fail_times=99, captured_prompts=captured)

        # First pass fails → requeue. Second pass rebuilds the prompt.
        await self._run_once(executor, task.id, agent.id, router, settings)
        await self._run_once(executor, task.id, agent.id, router, settings)

        # The first prompt had no rejection section; the second one does.
        assert "Why the last attempt was rejected" not in captured[0]
        second = captured[1]
        assert "Why the last attempt was rejected" in second
        # The specific unmet criteria text appears in the feedback section.
        for criterion in _SUCCESS_CRITERIA:
            assert criterion in second

    @pytest.mark.asyncio
    async def test_loop_converges_when_agent_finally_succeeds(self, svl_singletons):
        """If the agent fails once then produces a passing result on the
        requeue, the task converges to DONE within budget — no escalation."""

        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_budget(True, 2)
        router = _svl_converging_router(fail_times=1)

        # Pass 1: fails → requeue to ASSIGNED.
        await self._run_once(executor, task.id, agent.id, router, settings)
        mid = await manager.get_task(task.id)
        assert mid.status == TaskStatus.ASSIGNED
        assert mid.metadata["verify_requeue_count"] == 1

        # Pass 2: now passes → DONE, no escalation, counter unchanged.
        await self._run_once(executor, task.id, agent.id, router, settings)
        done = await manager.get_task(task.id)
        assert done.status == TaskStatus.DONE
        assert done.metadata["verify_verdict"]["status"] == "solved"
        assert "verify_escalation_reason" not in done.metadata
        assert done.metadata["verify_requeue_count"] == 1

    @pytest.mark.asyncio
    async def test_solved_task_goes_straight_to_done_no_requeue(self, svl_singletons):
        """A passing (SOLVED) result is NEVER requeued or mutated: status DONE,
        no requeue counter, no feedback, no escalation."""

        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_budget(True, 2)
        router = _svl_mock_router()  # always emits the passing output

        result = await self._run_once(executor, task.id, agent.id, router, settings)
        assert result["status"] == "completed"

        done = await manager.get_task(task.id)
        assert done.status == TaskStatus.DONE
        assert done.metadata["verify_verdict"]["status"] == "solved"
        assert done.metadata.get("verify_requeue_count", 0) == 0
        assert "verify_feedback" not in done.metadata
        assert "verify_escalation_reason" not in done.metadata

    @pytest.mark.asyncio
    async def test_flag_off_never_requeues_a_failing_task(self, svl_singletons):
        """With the flag OFF a failing result completes DONE exactly as before —
        no verdict, no requeue, no escalation. SVL-2 is fully inert."""

        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_budget(False, 2)
        router = _svl_failing_router()

        result = await self._run_once(executor, task.id, agent.id, router, settings)
        assert result["status"] == "completed"

        done = await manager.get_task(task.id)
        assert done.status == TaskStatus.DONE
        assert "verify_verdict" not in done.metadata
        assert "verify_requeue_count" not in done.metadata
        assert "verify_feedback" not in done.metadata
        assert "verify_escalation_reason" not in done.metadata

    @pytest.mark.asyncio
    async def test_real_redispatch_chain_auto_requeues_to_blocked(self, svl_singletons):
        """End-to-end through the REAL re-dispatch path: launch via
        ``execute_task_background`` and let the production
        ``asyncio.create_task`` chain run. The loop must auto-requeue itself and
        settle on BLOCKED without the test driving each pass."""

        manager, agent, task = await self._make_budget_agent_and_task()
        executor = get_mc_task_executor()
        max_requeues = 2
        settings = _settings_with_verify_budget(True, max_requeues)
        # Shrinking-but-never-solved so the chain settles on BUDGET exhaustion
        # rather than the SVL-3 no-progress early escalation.
        router = _svl_shrinking_failing_router()

        with (
            patch(
                "pocketpaw.mission_control.executor.AgentRouter",
                return_value=router,
            ),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=settings,
            ),
            patch("pocketpaw.mission_control.executor.get_message_bus") as mock_bus,
        ):
            mock_bus.return_value.publish_system = AsyncMock()
            launched = await executor.execute_task_background(task.id, agent.id)
            assert launched is True

            # Poll until the self-requeuing chain settles on a terminal state.
            for _ in range(200):
                await asyncio.sleep(0.02)
                current = await manager.get_task(task.id)
                if (
                    current.status == TaskStatus.BLOCKED
                    and current.id not in executor._running_tasks
                ):
                    break
            else:  # pragma: no cover - only hit if the loop never settles
                pytest.fail(
                    f"requeue chain did not settle; last status={current.status}, "
                    f"requeues={current.metadata.get('verify_requeue_count')}"
                )

        settled = await manager.get_task(task.id)
        assert settled.status == TaskStatus.BLOCKED
        assert settled.metadata["verify_escalation_reason"] == "budget_exhausted"
        assert settled.metadata["verify_requeue_count"] == max_requeues


# ---------------------------------------------------------------------------
# SVL-3: no-progress / oscillation guard
# ---------------------------------------------------------------------------

# An output that satisfies EXACTLY ONE of the two _SUCCESS_CRITERIA — it names
# the row/amount/customer/email criterion in full but never produces the
# overdue-invoice list. So the unmet set shrinks from {both} to {criterion-1}.
_PARTIAL_OUTPUT = (
    "Each row carries an amount and a customer email address, "
    "but I could not produce the overdue list."
)


def _svl_scripted_router(outputs: list[str], *, captured_prompts: list[str] | None = None):
    """A mock AgentRouter that yields ``outputs[i]`` on the i-th call, clamping
    to the last entry once the list is exhausted. Lets a test script the exact
    unmet-criteria trajectory across requeues (same set vs. shrinking set)."""
    state = {"calls": 0}

    async def mock_run(prompt):
        if captured_prompts is not None:
            captured_prompts.append(prompt)
        idx = min(state["calls"], len(outputs) - 1)
        state["calls"] += 1
        yield AgentEvent(type="message", content=outputs[idx])
        yield AgentEvent(type="done", content="")
        await asyncio.sleep(0)

    router = MagicMock()
    router.run = mock_run
    router.stop = AsyncMock()
    return router


class TestVerifyNoProgress:
    """SVL-3: a task that keeps failing the SAME criteria (or whose unmet set is
    not shrinking) is escalated to BLOCKED EARLY — before the requeue budget is
    spent. A task making progress (strictly shrinking unmet set) keeps requeuing
    normally and is never escalated for no-progress."""

    async def _make_agent_and_task(self):
        from pocketpaw.mission_control import get_mission_control_manager

        manager = get_mission_control_manager()
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
        task.metadata["success_criteria"] = list(_SUCCESS_CRITERIA)
        await manager.save_task(task)
        await manager.assign_task(task.id, [agent.id])
        return manager, agent, task

    async def _run_once(self, executor, task_id, agent_id, mock_router, settings):
        with (
            patch(
                "pocketpaw.mission_control.executor.AgentRouter",
                return_value=mock_router,
            ),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=settings,
            ),
            patch("pocketpaw.mission_control.executor.get_message_bus") as mock_bus,
        ):
            mock_bus.return_value.publish_system = AsyncMock()
            return await executor.execute_task(task_id, agent_id)

    @pytest.mark.asyncio
    async def test_same_failure_escalates_before_budget(self, svl_singletons):
        """Two consecutive attempts that fail the SAME criteria escalate to
        BLOCKED with ``verify_escalation_reason == "no_progress"`` BEFORE the
        (generous) requeue budget is reached — proving it escalated on stuck,
        not on budget."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        # Generous budget so a budget-exhaustion escalation is impossible this
        # early; any BLOCKED here MUST be the no-progress guard.
        max_requeues = 5
        settings = _settings_with_verify_budget(True, max_requeues)
        # Always the same failing output → identical unmet set every attempt.
        router = _svl_failing_router()

        # Pass 1: first failure, no prior attempt to compare → normal requeue.
        await self._run_once(executor, task.id, agent.id, router, settings)
        after_first = await manager.get_task(task.id)
        assert after_first.status == TaskStatus.ASSIGNED
        assert after_first.metadata["verify_requeue_count"] == 1
        assert "verify_escalation_reason" not in after_first.metadata

        # Pass 2: same unmet set as pass 1 → no progress → escalate EARLY.
        await self._run_once(executor, task.id, agent.id, router, settings)
        blocked = await manager.get_task(task.id)
        assert blocked.status == TaskStatus.BLOCKED
        assert blocked.metadata["verify_escalation_reason"] == "no_progress"
        # Escalated EARLY: the counter is still 1, far below the budget of 5.
        assert blocked.metadata["verify_requeue_count"] == 1
        assert blocked.metadata["verify_requeue_count"] < max_requeues

    @pytest.mark.asyncio
    async def test_no_progress_escalation_reports_its_reason(self, svl_singletons):
        """The escalation activity + broadcast reflect the no_progress cause,
        not the budget-exhaustion wording."""
        from pocketpaw.mission_control import ActivityType

        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_budget(True, 5)
        router = _svl_failing_router()

        # Capture the broadcast events on the escalating (second) pass.
        await self._run_once(executor, task.id, agent.id, router, settings)

        captured_events: list[tuple[str, dict]] = []

        async def _capture(event_type, data):
            captured_events.append((event_type, data))

        with patch.object(executor, "_broadcast_event", new=_capture):
            await self._run_once(executor, task.id, agent.id, router, settings)

        blocked = await manager.get_task(task.id)
        assert blocked.status == TaskStatus.BLOCKED
        assert blocked.metadata["verify_escalation_reason"] == "no_progress"

        # The escalation activity names the no-progress cause and asks for review.
        feed = await manager.get_activity_feed(limit=100)
        escalations = [
            a
            for a in feed
            if a.task_id == task.id
            and a.type == ActivityType.TASK_UPDATED
            and "escalated" in a.message
        ]
        assert escalations, "expected a needs-review escalation activity"
        msg = escalations[0].message
        assert "needs review" in msg
        assert "no progress" in msg
        # And it does NOT claim the budget was exhausted.
        assert "requeue(s)" not in msg

        # The mc_task_blocked broadcast carries the no_progress reason.
        blocked_events = [d for (t, d) in captured_events if t == "mc_task_blocked"]
        assert blocked_events, "expected an mc_task_blocked broadcast"
        assert blocked_events[0]["escalation_reason"] == "no_progress"

    @pytest.mark.asyncio
    async def test_progressing_task_is_not_escalated_early(self, svl_singletons):
        """A task whose unmet set strictly shrinks (2 unmet → 1 unmet → solved)
        keeps requeuing and converges to DONE — the no-progress guard must NOT
        fire while real progress is being made."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_budget(True, 5)
        # Attempt 1: both criteria unmet. Attempt 2: one criterion now met (set
        # shrank). Attempt 3: passing output → solved.
        router = _svl_scripted_router([_FAILING_OUTPUT, _PARTIAL_OUTPUT, _SUCCESS_OUTPUT])

        # Pass 1: 2 unmet, first failure → requeue.
        await self._run_once(executor, task.id, agent.id, router, settings)
        after_1 = await manager.get_task(task.id)
        assert after_1.status == TaskStatus.ASSIGNED
        assert after_1.metadata["verify_requeue_count"] == 1
        assert "verify_escalation_reason" not in after_1.metadata

        # Pass 2: 1 unmet — strictly fewer than the prior 2 → progress →
        # requeue again, NOT escalated.
        await self._run_once(executor, task.id, agent.id, router, settings)
        after_2 = await manager.get_task(task.id)
        assert after_2.status == TaskStatus.ASSIGNED, (
            "a shrinking unmet set must keep requeuing, not escalate"
        )
        assert after_2.metadata["verify_requeue_count"] == 2
        assert "verify_escalation_reason" not in after_2.metadata

        # Pass 3: all criteria met → DONE, no escalation ever stamped.
        await self._run_once(executor, task.id, agent.id, router, settings)
        done = await manager.get_task(task.id)
        assert done.status == TaskStatus.DONE
        assert done.metadata["verify_verdict"]["status"] == "solved"
        assert "verify_escalation_reason" not in done.metadata


# ---------------------------------------------------------------------------
# J-1: LLM-judge SHADOW stamp — the judge observes, never acts
# ---------------------------------------------------------------------------

import logging  # noqa: E402

from pocketpaw.instinct.judge_provider import LlmJudgeVerdictProvider  # noqa: E402

# A judge decision that marks BOTH _SUCCESS_CRITERIA as met at high
# confidence — the "judge PASS" half of the disagreement scenario.
_JUDGE_ALL_MET = json.dumps(
    {
        "criteria": [
            {"criterion": "c1", "met": True, "reason": "list produced"},
            {"criterion": "c2", "met": True, "reason": "rows carry amount + email"},
        ],
        "confidence": 0.95,
    }
)


class _FakeJudgeLlm:
    """Deterministic judge transport — no real ``claude`` subprocess ever."""

    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    async def judge(self, *, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def _settings_with_judge(loop_enabled: bool, judge_enabled: bool, max_requeues: int = 2):
    """A Settings copy with the verify-loop AND judge-shadow flags forced."""
    return get_settings().model_copy(
        update={
            "deep_work_verify_loop_enabled": loop_enabled,
            "deep_work_verify_max_requeues": max_requeues,
            "deep_work_verify_judge_shadow_enabled": judge_enabled,
        }
    )


class TestJudgeShadow:
    """J-1: the judge verdict is stamped + logged alongside the deterministic
    one but NEVER drives requeue/escalate/DONE — pure observation."""

    async def _make_agent_and_task(self):
        from pocketpaw.mission_control import get_mission_control_manager

        manager = get_mission_control_manager()
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
        task.metadata["success_criteria"] = list(_SUCCESS_CRITERIA)
        await manager.save_task(task)
        await manager.assign_task(task.id, [agent.id])
        return manager, agent, task

    async def _run_once(self, executor, task_id, agent_id, mock_router, settings, judge_cls):
        """One execute_task pass with the verify settings AND the judge
        provider class patched (the fake transport rides inside judge_cls)."""
        with (
            patch(
                "pocketpaw.mission_control.executor.AgentRouter",
                return_value=mock_router,
            ),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=settings,
            ),
            patch(
                "pocketpaw.mission_control.executor.LlmJudgeVerdictProvider",
                judge_cls,
            ),
            patch("pocketpaw.mission_control.executor.get_message_bus") as mock_bus,
        ):
            mock_bus.return_value.publish_system = AsyncMock()
            return await executor.execute_task(task_id, agent_id)

    @pytest.mark.asyncio
    async def test_judge_pass_never_rescues_a_deterministic_fail(self, svl_singletons, caplog):
        """The high-value disagreement: deterministic says NOT_SOLVED, judge
        says SOLVED. The task must STILL requeue (the judge did not rescue
        it), the judge verdict must be stamped, and the agree=False shadow
        log line must fire."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_judge(loop_enabled=True, judge_enabled=True)

        fake_llm = _FakeJudgeLlm(response=_JUDGE_ALL_MET)
        judge_cls = MagicMock(return_value=LlmJudgeVerdictProvider(llm=fake_llm))
        router = _svl_failing_router()  # output fails BOTH criteria

        with caplog.at_level(logging.INFO, logger="pocketpaw.mission_control.executor"):
            await self._run_once(executor, task.id, agent.id, router, settings, judge_cls)

        reloaded = await manager.get_task(task.id)
        # The deterministic verdict alone drove behaviour: requeued, not DONE.
        assert reloaded.status == TaskStatus.ASSIGNED, (
            "a judge PASS must never rescue a deterministically-failing task"
        )
        assert reloaded.metadata["verify_verdict"]["status"] == "not_solved"
        assert reloaded.metadata["verify_requeue_count"] == 1

        # The judge verdict was stamped observe-only, disagreeing.
        judge_verdict = reloaded.metadata.get("verify_judge_verdict")
        assert judge_verdict is not None, "verify_judge_verdict was not stamped"
        assert judge_verdict["status"] == "solved"

        # Exactly one judge call, and the structured disagreement line fired.
        assert len(fake_llm.prompts) == 1
        assert "verify judge shadow: deterministic=not_solved judge=solved" in caplog.text
        assert "agree=False" in caplog.text

    @pytest.mark.asyncio
    async def test_judge_flag_off_no_stamp_and_provider_never_constructed(self, svl_singletons):
        """Loop on, judge OFF: the loop stamps its deterministic verdict as
        today, the judge provider is never even constructed (⇒ no subprocess
        could spawn), and no judge verdict is stamped."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_judge(loop_enabled=True, judge_enabled=False)

        judge_cls = MagicMock()
        router = _svl_mock_router()  # passing output → DONE

        await self._run_once(executor, task.id, agent.id, router, settings, judge_cls)

        done = await manager.get_task(task.id)
        assert done.status == TaskStatus.DONE
        assert done.metadata["verify_verdict"]["status"] == "solved"
        assert "verify_judge_verdict" not in done.metadata
        judge_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_judge_flag_without_loop_flag_runs_nothing(self, svl_singletons):
        """Judge on, loop OFF: the judge requires the loop — nothing runs.
        No deterministic verdict, no judge verdict, provider never built."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_judge(loop_enabled=False, judge_enabled=True)

        judge_cls = MagicMock()
        router = _svl_mock_router()

        await self._run_once(executor, task.id, agent.id, router, settings, judge_cls)

        done = await manager.get_task(task.id)
        assert done.status == TaskStatus.DONE
        assert "verify_verdict" not in done.metadata
        assert "verify_judge_verdict" not in done.metadata
        judge_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_judge_exception_never_breaks_completion(self, svl_singletons):
        """A judge that blows up entirely (even past the provider's own
        fail-safe) is swallowed by the shadow hook — the task completes DONE
        exactly as if the judge had never run."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_judge(loop_enabled=True, judge_enabled=True)

        broken_provider = MagicMock()
        broken_provider.verify = AsyncMock(side_effect=RuntimeError("judge exploded"))
        judge_cls = MagicMock(return_value=broken_provider)
        router = _svl_mock_router()

        result = await self._run_once(executor, task.id, agent.id, router, settings, judge_cls)
        assert result["status"] == "completed"

        done = await manager.get_task(task.id)
        assert done.status == TaskStatus.DONE
        assert done.metadata["verify_verdict"]["status"] == "solved"
        # The broken judge left no stamp — and broke nothing.
        assert "verify_judge_verdict" not in done.metadata
        broken_provider.verify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_agreement_case_stamps_and_logs_agree_true(self, svl_singletons, caplog):
        """Deterministic SOLVED + judge SOLVED: task DONE as today, both
        verdicts stamped, agree=True logged."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_judge(loop_enabled=True, judge_enabled=True)

        fake_llm = _FakeJudgeLlm(response=_JUDGE_ALL_MET)
        judge_cls = MagicMock(return_value=LlmJudgeVerdictProvider(llm=fake_llm))
        router = _svl_mock_router()  # passing output → deterministic SOLVED

        with caplog.at_level(logging.INFO, logger="pocketpaw.mission_control.executor"):
            await self._run_once(executor, task.id, agent.id, router, settings, judge_cls)

        done = await manager.get_task(task.id)
        assert done.status == TaskStatus.DONE
        assert done.metadata["verify_verdict"]["status"] == "solved"
        assert done.metadata["verify_judge_verdict"]["status"] == "solved"
        assert "verify judge shadow: deterministic=solved judge=solved" in caplog.text
        assert "agree=True" in caplog.text


# ---------------------------------------------------------------------------
# verify_mode: the three-position rollout switch (off | shadow | enforce)
# ---------------------------------------------------------------------------


def _settings_with_verify_mode(
    mode: str,
    *,
    legacy_bool: bool = False,
    max_requeues: int = 2,
    judge_enabled: bool = False,
):
    """A Settings copy with the three-position mode + the legacy bool forced."""
    return get_settings().model_copy(
        update={
            "deep_work_verify_mode": mode,
            "deep_work_verify_loop_enabled": legacy_bool,
            "deep_work_verify_max_requeues": max_requeues,
            "deep_work_verify_judge_shadow_enabled": judge_enabled,
        }
    )


class TestVerifyShadowMode:
    """The shadow rung: verdicts + would_have telemetry are stamped but task
    status is NEVER touched — a failing task still completes DONE with its
    deliverable intact. The legacy bool alone still means ENFORCE."""

    async def _make_agent_and_task(self):
        from pocketpaw.mission_control import get_mission_control_manager

        manager = get_mission_control_manager()
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
        task.metadata["success_criteria"] = list(_SUCCESS_CRITERIA)
        await manager.save_task(task)
        await manager.assign_task(task.id, [agent.id])
        return manager, agent, task

    async def _run_once(self, executor, task_id, agent_id, mock_router, settings):
        with (
            patch(
                "pocketpaw.mission_control.executor.AgentRouter",
                return_value=mock_router,
            ),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=settings,
            ),
            patch("pocketpaw.mission_control.executor.get_message_bus") as mock_bus,
        ):
            mock_bus.return_value.publish_system = AsyncMock()
            return await executor.execute_task(task_id, agent_id)

    @pytest.mark.asyncio
    async def test_shadow_failing_task_lands_done_with_would_have(self, svl_singletons, caplog):
        """The heart of the rollout rung: a FAILING task in shadow mode still
        completes DONE with the deliverable saved, carries the verdict +
        would_have + shadow marker, and grows NO requeue counter / feedback
        — status and should_retry are never touched."""
        from pocketpaw.mission_control import ActivityType

        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_mode("shadow")
        router = _svl_failing_router()  # output fails BOTH criteria

        with caplog.at_level(logging.INFO, logger="pocketpaw.mission_control.executor"):
            result = await self._run_once(executor, task.id, agent.id, router, settings)
        assert result["status"] == "completed"

        done = await manager.get_task(task.id)
        # The task landed DONE despite failing verify — shadow never acts.
        assert done.status == TaskStatus.DONE
        assert done.output == _FAILING_OUTPUT
        # Verdict + shadow telemetry stamped.
        assert done.metadata["verify_verdict"]["status"] == "not_solved"
        assert done.metadata["verify_mode"] == "shadow"
        assert done.metadata["verify_would_have"] == "requeued"
        # NO enforce-side state was written.
        assert "verify_requeue_count" not in done.metadata
        assert "verify_feedback" not in done.metadata
        assert "verify_escalation_reason" not in done.metadata
        # The would_have telemetry line fired.
        assert "verify shadow mode: would_have=requeued" in caplog.text

        # DONE side-effects ran exactly as a normal completion: the
        # completion activity logged and the output saved as a deliverable.
        feed = await manager.get_activity_feed(limit=100)
        completed = [
            a for a in feed if a.task_id == task.id and a.type == ActivityType.TASK_COMPLETED
        ]
        assert completed, "shadow mode must not suppress the TASK_COMPLETED activity"
        docs = await manager.get_task_documents(task.id)
        assert docs, "shadow mode must not suppress the deliverable save"

    @pytest.mark.asyncio
    async def test_shadow_solved_task_stamps_would_have_done(self, svl_singletons):
        """SOLVED under shadow: would_have=done — telemetry covers every
        completion, not just the failing ones."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_mode("shadow")
        router = _svl_mock_router()  # passing output

        await self._run_once(executor, task.id, agent.id, router, settings)

        done = await manager.get_task(task.id)
        assert done.status == TaskStatus.DONE
        assert done.metadata["verify_verdict"]["status"] == "solved"
        assert done.metadata["verify_mode"] == "shadow"
        assert done.metadata["verify_would_have"] == "done"
        assert "verify_requeue_count" not in done.metadata

    @pytest.mark.asyncio
    async def test_shadow_keeps_the_judge_shadow_running(self, svl_singletons):
        """The judge shadow works as-is under shadow mode: its verdict is
        stamped while the task still completes DONE."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_mode("shadow", judge_enabled=True)

        fake_llm = _FakeJudgeLlm(response=_JUDGE_ALL_MET)
        judge_cls = MagicMock(return_value=LlmJudgeVerdictProvider(llm=fake_llm))
        router = _svl_failing_router()

        with (
            patch(
                "pocketpaw.mission_control.executor.AgentRouter",
                return_value=router,
            ),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=settings,
            ),
            patch(
                "pocketpaw.mission_control.executor.LlmJudgeVerdictProvider",
                judge_cls,
            ),
            patch("pocketpaw.mission_control.executor.get_message_bus") as mock_bus,
        ):
            mock_bus.return_value.publish_system = AsyncMock()
            await executor.execute_task(task.id, agent.id)

        done = await manager.get_task(task.id)
        assert done.status == TaskStatus.DONE  # shadow never requeues
        assert done.metadata["verify_verdict"]["status"] == "not_solved"
        assert done.metadata["verify_judge_verdict"]["status"] == "solved"
        assert len(fake_llm.prompts) == 1

    @pytest.mark.asyncio
    async def test_legacy_bool_alone_still_enforces(self, svl_singletons):
        """BACK-COMPAT: mode left at 'off' + the legacy bool True must run the
        FULL loop (the bool's shipped meaning) — a failing task requeues, it
        does not silently complete in shadow."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_mode("off", legacy_bool=True)
        router = _svl_failing_router()

        await self._run_once(executor, task.id, agent.id, router, settings)

        reloaded = await manager.get_task(task.id)
        assert reloaded.status == TaskStatus.ASSIGNED, (
            "legacy bool True must keep enforcing — never weaken to shadow"
        )
        assert reloaded.metadata["verify_requeue_count"] == 1
        assert "verify_mode" not in reloaded.metadata
        assert "verify_would_have" not in reloaded.metadata

    @pytest.mark.asyncio
    async def test_mode_enforce_without_bool_drives_the_loop(self, svl_singletons):
        """mode='enforce' alone (legacy bool off) drives exactly today's
        flag-on behaviour — the new switch fully supersedes the bool."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_mode("enforce", legacy_bool=False)
        router = _svl_failing_router()

        await self._run_once(executor, task.id, agent.id, router, settings)

        reloaded = await manager.get_task(task.id)
        assert reloaded.status == TaskStatus.ASSIGNED
        assert reloaded.metadata["verify_requeue_count"] == 1
        assert len(reloaded.metadata["verify_feedback"]) == 1

    @pytest.mark.asyncio
    async def test_mode_off_default_is_byte_for_byte_inert(self, svl_singletons):
        """mode='off' + bool False (the shipped default): no verdict, no
        shadow stamps, no requeue — exactly today's default path."""
        manager, agent, task = await self._make_agent_and_task()
        executor = get_mc_task_executor()
        settings = _settings_with_verify_mode("off", legacy_bool=False)
        router = _svl_failing_router()

        result = await self._run_once(executor, task.id, agent.id, router, settings)
        assert result["status"] == "completed"

        done = await manager.get_task(task.id)
        assert done.status == TaskStatus.DONE
        assert "verify_verdict" not in done.metadata
        assert "verify_mode" not in done.metadata
        assert "verify_would_have" not in done.metadata
        assert "verify_requeue_count" not in done.metadata
