# Tests for agents/plan_mode.py
# Created: 2026-02-07

import asyncio

import pytest

from pocketclaw.agents.plan_mode import (
    ExecutionPlan,
    PlanManager,
    PlanStatus,
    PlanStep,
    get_plan_manager,
)


@pytest.fixture
def manager():
    return PlanManager()


# ---------------------------------------------------------------------------
# PlanStep
# ---------------------------------------------------------------------------


class TestPlanStep:
    def test_shell_preview(self):
        step = PlanStep(tool_name="shell", tool_input={"command": "ls -la"})
        assert step.generate_preview() == "$ ls -la"

    def test_bash_preview(self):
        step = PlanStep(tool_name="Bash", tool_input={"command": "git status"})
        assert step.generate_preview() == "$ git status"

    def test_write_file_preview(self):
        step = PlanStep(
            tool_name="write_file",
            tool_input={"path": "/tmp/test.py", "content": "print('hello')"},
        )
        preview = step.generate_preview()
        assert "/tmp/test.py" in preview
        assert "print('hello')" in preview

    def test_edit_preview(self):
        step = PlanStep(tool_name="Edit", tool_input={"file_path": "/tmp/test.py"})
        assert "Edit /tmp/test.py" in step.generate_preview()

    def test_generic_tool_preview(self):
        step = PlanStep(tool_name="web_search", tool_input={"query": "python docs"})
        preview = step.generate_preview()
        assert "web_search" in preview
        assert "python docs" in preview


# ---------------------------------------------------------------------------
# ExecutionPlan
# ---------------------------------------------------------------------------


class TestExecutionPlan:
    def test_add_step(self):
        plan = ExecutionPlan(session_key="test-session")
        step = plan.add_step("shell", {"command": "echo hi"})
        assert len(plan.steps) == 1
        assert step.tool_name == "shell"
        assert step.preview == "$ echo hi"

    def test_to_preview(self):
        plan = ExecutionPlan(session_key="test")
        plan.add_step("shell", {"command": "ls"})
        plan.add_step("write_file", {"path": "/tmp/out.txt", "content": "data"})
        preview = plan.to_preview()
        assert "2 steps" in preview
        assert "$ ls" in preview
        assert "/tmp/out.txt" in preview

    def test_empty_preview(self):
        plan = ExecutionPlan(session_key="test")
        assert plan.to_preview() == "Empty plan"

    def test_to_dict(self):
        plan = ExecutionPlan(session_key="test")
        plan.add_step("shell", {"command": "ls"})
        d = plan.to_dict()
        assert d["session_key"] == "test"
        assert d["status"] == "proposed"
        assert len(d["steps"]) == 1
        assert d["steps"][0]["tool_name"] == "shell"


# ---------------------------------------------------------------------------
# PlanManager
# ---------------------------------------------------------------------------


class TestPlanManager:
    def test_create_plan(self, manager):
        plan = manager.create_plan("session-1")
        assert plan.session_key == "session-1"
        assert plan.status == PlanStatus.PROPOSED

    def test_add_step_creates_plan(self, manager):
        step = manager.add_step_to_plan("session-2", "shell", {"command": "ls"})
        assert step is not None
        plan = manager.get_active_plan("session-2")
        assert plan is not None
        assert len(plan.steps) == 1

    def test_approve_plan(self, manager):
        manager.create_plan("s1")
        result = manager.approve_plan("s1")
        assert result is not None
        assert result.status == PlanStatus.APPROVED

    def test_reject_plan(self, manager):
        manager.create_plan("s1")
        result = manager.reject_plan("s1")
        assert result is not None
        assert result.status == PlanStatus.REJECTED

    def test_approve_nonexistent(self, manager):
        assert manager.approve_plan("nope") is None

    def test_reject_nonexistent(self, manager):
        assert manager.reject_plan("nope") is None

    def test_get_active_plan_expired(self, manager):
        plan = manager.create_plan("s1")
        plan.created_at = 0  # expired
        assert manager.get_active_plan("s1") is None

    def test_clear_plan(self, manager):
        manager.create_plan("s1")
        manager.clear_plan("s1")
        assert manager.get_active_plan("s1") is None


# ---------------------------------------------------------------------------
# Async approval flow
# ---------------------------------------------------------------------------


async def test_wait_for_approval(manager):
    manager.create_plan("s1")

    async def approve_later():
        await asyncio.sleep(0.05)
        manager.approve_plan("s1")

    asyncio.create_task(approve_later())
    status = await manager.wait_for_approval("s1", timeout=2)
    assert status == PlanStatus.APPROVED


async def test_wait_for_rejection(manager):
    manager.create_plan("s1")

    async def reject_later():
        await asyncio.sleep(0.05)
        manager.reject_plan("s1")

    asyncio.create_task(reject_later())
    status = await manager.wait_for_approval("s1", timeout=2)
    assert status == PlanStatus.REJECTED


async def test_wait_timeout(manager):
    manager.create_plan("s1")
    with pytest.raises(asyncio.TimeoutError):
        await manager.wait_for_approval("s1", timeout=0.05)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_singleton():
    m1 = get_plan_manager()
    m2 = get_plan_manager()
    assert m1 is m2
