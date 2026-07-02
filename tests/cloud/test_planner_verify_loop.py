# Created: 2026-07-02 (feat/svl-5-cloud-verify) — SVL-5: the Self-Verifying
#   Loop at the CLOUD planner terminal. Drives the real
#   ``_execute_ready_plan_tasks`` → ``_run_one`` → ``_verify_plan_task_outcome``
#   path against mongomock Beanie docs with a fake AgentPool that scripts one
#   output per attempt, and asserts the loop's contract:
#     - SOLVED → done untouched, verdict stamped, output file saved as today;
#     - failing-but-progressing → requeued (status back to ``proposed``) with
#       the unmet criteria rendered into the SECOND run's instructions,
#       bounded by ``cloud_plan_verify_max_requeues``, then ``failed`` with
#       ``verify.escalation_reason == 'budget_exhausted'``;
#     - identical unmet set on two consecutive attempts → ``failed`` with
#       ``verify.escalation_reason == 'no_progress'`` BEFORE the budget;
#     - flag off → no verdict, no requeue, byte-for-byte today's behaviour;
#     - a requeued or escalated attempt NEVER saves the output file, uploads
#       artifacts, or emits the done-completion (TaskResolved status=done).
"""SVL-5 — Self-Verifying Loop at the cloud planner terminal."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud._core.realtime.events import TaskResolved, TaskUpdated
from pocketpaw_ee.cloud.models.task import Task as TaskDoc
from pocketpaw_ee.cloud.models.task import TaskAssignee as TaskAssigneeDoc
from pocketpaw_ee.cloud.planner import service as planner_service

from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import get_settings

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "w1"
_PROJECT = "p-verify"
_AGENT_ID = "agent-1"

# Deterministic-verifier-friendly criteria: each keys off one distinctive
# token ("alpha"/"bravo"/"charlie") plus the shared "mentions" stem, so a
# scripted output meets exactly the criteria whose keyword it contains.
_C_ALPHA = "mentions alpha"
_C_BRAVO = "mentions bravo"
_C_CHARLIE = "mentions charlie"


class _FakePool:
    """AgentPool stand-in scripting one output per run; records instructions.

    ``outputs[i]`` is what attempt ``i+1`` streams back; the last entry
    repeats if the loop runs longer than scripted (defensive — a correct
    loop never does). ``calls`` captures each run's ``message`` so tests
    can assert the verify feedback landed in the re-run's instructions.
    """

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[str] = []

    def run(self, *, agent_id: str, message: str, session_key: str, instructions: str):  # noqa: ARG002
        self.calls.append(message)
        content = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]

        async def _gen():
            yield AgentEvent(type="message", content=content)
            yield AgentEvent(type="done", content="")

        return _gen()


def _settings(enabled: bool, max_requeues: int = 2):
    """A Settings copy with the SVL-5 flag and requeue budget forced."""
    return get_settings().model_copy(
        update={
            "cloud_plan_verify_loop_enabled": enabled,
            "cloud_plan_verify_max_requeues": max_requeues,
        }
    )


async def _make_plan_task(criteria: list[str]) -> str:
    doc = TaskDoc(
        workspace_id=_WS,
        project_id=_PROJECT,
        creator_id="u1",
        title="Pull the vendor report",
        summary="Compile the report the plan asked for.",
        assignee=TaskAssigneeDoc(kind="agent", id=_AGENT_ID, name="PlanBot"),
        assignee_id=_AGENT_ID,
        assignee_kind="agent",
        status="proposed",
        success_criteria=list(criteria),
    )
    await doc.insert()
    return str(doc.id)


async def _drive(pool: _FakePool, *, enabled: bool, max_requeues: int = 2):
    """Run the real dispatch path with the pool, settings, and DONE
    side-effects (output-file save + artifact scan) patched. Returns the
    two side-effect mocks so tests assert fired / not-fired."""
    save_mock = AsyncMock()
    scan_mock = AsyncMock(return_value=[])
    with (
        patch("pocketpaw.agents.pool.get_agent_pool", return_value=pool),
        patch(
            "pocketpaw.config.get_settings",
            return_value=_settings(enabled, max_requeues),
        ),
        patch.object(planner_service, "_save_task_output_file", save_mock),
        patch.object(planner_service, "_scan_and_upload_agent_files", scan_mock),
    ):
        await planner_service._execute_ready_plan_tasks(workspace_id=_WS, project_id=_PROJECT)
    return save_mock, scan_mock


def _resolved_done_events(bus) -> list:
    return [
        e for e in bus.events if isinstance(e, TaskResolved) and e.data["task"]["status"] == "done"
    ]


# ---------------------------------------------------------------------------
# SOLVED / UNKNOWN pass through
# ---------------------------------------------------------------------------


async def test_solved_completes_as_today_with_verdict_stamped(recording_bus) -> None:
    """A passing result is never requeued or mutated: one run, status done,
    verdict stamped on ``Task.verify``, output file saved as today."""

    tid = await _make_plan_task([_C_ALPHA])
    pool = _FakePool(["The summary mentions alpha clearly."])

    save_mock, scan_mock = await _drive(pool, enabled=True)

    doc = await TaskDoc.get(tid)
    assert len(pool.calls) == 1
    assert doc.status == "done"
    assert doc.verify["verdict"]["status"] == "solved"
    assert "feedback" not in doc.verify
    assert "escalation_reason" not in doc.verify
    # DONE side-effects fired exactly as today.
    assert save_mock.await_count == 1
    assert save_mock.await_args.args[3] == "The summary mentions alpha clearly."
    assert scan_mock.await_count == 1
    assert len(_resolved_done_events(recording_bus)) == 1


async def test_unknown_no_criteria_passes_through(recording_bus) -> None:
    """No criteria captured → UNKNOWN → complete exactly as today."""

    tid = await _make_plan_task([])
    pool = _FakePool(["Some output with no checkable contract."])

    save_mock, _ = await _drive(pool, enabled=True)

    doc = await TaskDoc.get(tid)
    assert len(pool.calls) == 1
    assert doc.status == "done"
    assert doc.verify["verdict"]["status"] == "unknown"
    assert save_mock.await_count == 1
    assert len(_resolved_done_events(recording_bus)) == 1


# ---------------------------------------------------------------------------
# Requeue with feedback, then budget exhaustion
# ---------------------------------------------------------------------------


async def test_failing_requeues_with_feedback_then_budget_exhausts(recording_bus) -> None:
    """Shrinking-but-never-solved: each attempt meets one more criterion so
    the no-progress guard never fires, the task requeues exactly
    ``max_requeues`` times with the prior rejections in the re-run's
    instructions, then lands ``failed`` with reason=budget_exhausted. A
    rejected attempt never fires the DONE side-effects."""

    tid = await _make_plan_task([_C_ALPHA, _C_BRAVO, _C_CHARLIE])
    pool = _FakePool(
        [
            "The agent mentions nothing useful here.",  # unmet: alpha,bravo,charlie
            "The report mentions alpha throughout.",  # unmet: bravo,charlie
            "The report mentions alpha and bravo now.",  # unmet: charlie
        ]
    )

    save_mock, scan_mock = await _drive(pool, enabled=True, max_requeues=2)

    doc = await TaskDoc.get(tid)
    assert len(pool.calls) == 3  # initial + 2 requeues
    assert doc.status == "failed"
    assert doc.verify["escalation_reason"] == "budget_exhausted"
    # Verify counter is its own budget — bumped once per requeue only.
    assert doc.verify["requeue_count"] == 2
    assert [f["attempt"] for f in doc.verify["feedback"]] == [1, 2]
    assert doc.verify["verdict"]["status"] == "partial"  # last attempt: 2/3 met

    # The SECOND run's instructions carry attempt 1's specific rejections.
    assert "Why the last attempt was rejected" not in pool.calls[0]
    assert "Why the last attempt was rejected" in pool.calls[1]
    for criterion in (_C_ALPHA, _C_BRAVO, _C_CHARLIE):
        assert criterion in pool.calls[1]
    # The THIRD run carries the cumulative log (attempts 1 and 2).
    assert "Attempt 1" in pool.calls[2]
    assert "Attempt 2" in pool.calls[2]

    # No DONE side-effect ever fired for the rejected attempts.
    assert save_mock.await_count == 0
    assert scan_mock.await_count == 0
    assert _resolved_done_events(recording_bus) == []
    # Each requeue announced the task back at ``proposed``; the escalation
    # emitted TaskResolved (status=failed) so the dependent cascade fires.
    requeue_events = [
        e
        for e in recording_bus.events
        if isinstance(e, TaskUpdated) and e.data["task"]["status"] == "proposed"
    ]
    assert len(requeue_events) == 2
    failed_events = [
        e
        for e in recording_bus.events
        if isinstance(e, TaskResolved) and e.data["task"]["status"] == "failed"
    ]
    assert len(failed_events) == 1


# ---------------------------------------------------------------------------
# No-progress guard escalates before the budget
# ---------------------------------------------------------------------------


async def test_identical_unmet_set_twice_escalates_no_progress(recording_bus) -> None:
    """Two consecutive attempts failing the exact same criterion escalate
    to ``failed`` with reason=no_progress BEFORE the requeue budget is
    spent (budget is 2; only 1 requeue happened)."""

    tid = await _make_plan_task([_C_ALPHA])
    pool = _FakePool(
        [
            "This mentions bravo.",  # unmet: alpha
            "Still mentions bravo.",  # unmet: alpha — identical set
        ]
    )

    save_mock, scan_mock = await _drive(pool, enabled=True, max_requeues=2)

    doc = await TaskDoc.get(tid)
    assert len(pool.calls) == 2
    assert doc.status == "failed"
    assert doc.verify["escalation_reason"] == "no_progress"
    assert doc.verify["requeue_count"] == 1  # budget of 2 NOT exhausted
    assert save_mock.await_count == 0
    assert scan_mock.await_count == 0
    assert _resolved_done_events(recording_bus) == []


# ---------------------------------------------------------------------------
# Flag off — byte-for-byte today's behaviour
# ---------------------------------------------------------------------------


async def test_flag_off_no_verdict_no_requeue(recording_bus) -> None:
    """Loop disabled: a failing output completes exactly as before — one
    run, no verdict stamped, no feedback section in the instructions,
    DONE side-effects fire."""

    tid = await _make_plan_task([_C_ALPHA])
    pool = _FakePool(["This mentions bravo."])  # would fail the criterion

    save_mock, scan_mock = await _drive(pool, enabled=False)

    doc = await TaskDoc.get(tid)
    assert len(pool.calls) == 1
    assert doc.status == "done"
    assert doc.verify == {}
    assert "Why the last attempt was rejected" not in pool.calls[0]
    assert save_mock.await_count == 1
    assert scan_mock.await_count == 1
    assert len(_resolved_done_events(recording_bus)) == 1
