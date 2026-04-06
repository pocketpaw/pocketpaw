# Tests for SimulationClock and tick-synchronized scheduler dispatch.
# Created: 2026-03-26
#
# Covers:
# - SimulationClock: advance, elapsed, reset, wait_for_tick, snapshots
# - TickSnapshot: to_dict / from_dict round-trip
# - DependencyScheduler.run_tick_synchronized: tick-stepped dispatch with snapshots
# - MCTaskExecutor._build_task_prompt: simulation_tick metadata injection

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.deep_work.clock import SimulationClock, TickSnapshot
from pocketpaw.deep_work.scheduler import DependencyScheduler
from pocketpaw.mission_control.models import Task, TaskPriority, TaskStatus

# ============================================================================
# SimulationClock
# ============================================================================


class TestSimulationClock:
    async def test_initial_tick_is_zero(self):
        clock = SimulationClock()
        assert clock.current_tick == 0
        assert clock.elapsed() == 0

    async def test_advance_increments_tick(self):
        clock = SimulationClock()
        new_tick = await clock.advance()
        assert new_tick == 1
        assert clock.current_tick == 1
        assert clock.elapsed() == 1

    async def test_advance_multiple_times(self):
        clock = SimulationClock()
        for expected in range(1, 6):
            tick = await clock.advance()
            assert tick == expected
        assert clock.current_tick == 5

    async def test_reset_clears_tick_and_snapshots(self):
        clock = SimulationClock()
        await clock.advance()
        await clock.advance()
        clock.record_snapshot(TickSnapshot(tick=1, task_states={"t1": "done"}))
        clock.reset()
        assert clock.current_tick == 0
        assert clock.get_snapshots() == []

    async def test_record_and_get_snapshots(self):
        clock = SimulationClock()
        snap1 = TickSnapshot(tick=0, task_states={"t1": "inbox"})
        snap2 = TickSnapshot(tick=1, task_states={"t1": "done"})
        clock.record_snapshot(snap1)
        clock.record_snapshot(snap2)
        assert len(clock.get_snapshots()) == 2
        assert clock.get_snapshots()[0].tick == 0
        assert clock.get_snapshots()[1].tick == 1

    async def test_get_snapshot_at(self):
        clock = SimulationClock()
        snap = TickSnapshot(tick=3, task_states={"t1": "done"})
        clock.record_snapshot(snap)
        assert clock.get_snapshot_at(3) is not None
        assert clock.get_snapshot_at(3).tick == 3
        assert clock.get_snapshot_at(99) is None

    async def test_get_snapshots_returns_copy(self):
        """Mutating the returned list should not affect internal state."""
        clock = SimulationClock()
        clock.record_snapshot(TickSnapshot(tick=0))
        snaps = clock.get_snapshots()
        snaps.clear()
        assert len(clock.get_snapshots()) == 1


# ============================================================================
# TickSnapshot
# ============================================================================


class TestTickSnapshot:
    def test_to_dict(self):
        snap = TickSnapshot(tick=5, task_states={"t1": "done"}, metadata={"key": "val"})
        d = snap.to_dict()
        assert d == {"tick": 5, "task_states": {"t1": "done"}, "metadata": {"key": "val"}}

    def test_from_dict(self):
        data = {"tick": 3, "task_states": {"t2": "inbox"}, "metadata": {"x": 1}}
        snap = TickSnapshot.from_dict(data)
        assert snap.tick == 3
        assert snap.task_states == {"t2": "inbox"}
        assert snap.metadata == {"x": 1}

    def test_round_trip(self):
        original = TickSnapshot(tick=7, task_states={"a": "done", "b": "inbox"}, metadata={"n": 2})
        restored = TickSnapshot.from_dict(original.to_dict())
        assert restored.tick == original.tick
        assert restored.task_states == original.task_states
        assert restored.metadata == original.metadata

    def test_from_dict_defaults(self):
        snap = TickSnapshot.from_dict({})
        assert snap.tick == 0
        assert snap.task_states == {}
        assert snap.metadata == {}


# ============================================================================
# DependencyScheduler — tick-synchronized dispatch
# ============================================================================


def _make_task(
    task_id: str,
    status: TaskStatus = TaskStatus.INBOX,
    project_id: str = "proj-1",
    blocked_by: list[str] | None = None,
    task_type: str = "agent",
    assignee_ids: list[str] | None = None,
    title: str = "",
) -> Task:
    return Task(
        id=task_id,
        title=title or f"Task {task_id}",
        status=status,
        project_id=project_id,
        blocked_by=blocked_by or [],
        task_type=task_type,
        assignee_ids=assignee_ids or [],
    )


@pytest.fixture
def mock_manager():
    manager = AsyncMock()
    manager.list_tasks = AsyncMock(return_value=[])
    manager.get_project_tasks = AsyncMock(return_value=[])
    manager.get_task = AsyncMock(return_value=None)
    manager.get_project = AsyncMock(return_value=None)
    manager.update_project = AsyncMock()
    manager.save_task = AsyncMock()
    return manager


@pytest.fixture
def mock_executor():
    executor = AsyncMock()
    executor.execute_task_background = AsyncMock()
    executor.is_task_running = MagicMock(return_value=False)
    return executor


@pytest.fixture
def clock():
    return SimulationClock()


class TestRunTickSynchronized:
    async def test_raises_without_clock(self, mock_manager, mock_executor):
        scheduler = DependencyScheduler(mock_manager, mock_executor)
        with pytest.raises(RuntimeError, match="SimulationClock"):
            await scheduler.run_tick_synchronized("proj-1")

    async def test_empty_project_returns_no_snapshots(self, mock_manager, mock_executor, clock):
        """A project with no ready tasks yields no snapshots."""
        mock_manager.get_project_tasks.return_value = []
        scheduler = DependencyScheduler(mock_manager, mock_executor, clock=clock)

        snapshots = await scheduler.run_tick_synchronized("proj-1")

        assert snapshots == []
        assert clock.current_tick == 0

    async def test_single_tick_dispatches_all_ready_tasks(self, mock_manager, mock_executor, clock):
        """All unblocked tasks dispatch in one tick, then clock advances."""
        t1 = _make_task("t1", assignee_ids=["a1"])
        t2 = _make_task("t2", assignee_ids=["a2"])

        call_count = 0

        async def fake_get_project_tasks(pid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # First call: get_ready_tasks returns t1+t2
                # Second call: snapshot recording
                return [t1, t2]
            # After dispatch, tasks are done
            t1_done = _make_task("t1", status=TaskStatus.DONE, assignee_ids=["a1"])
            t2_done = _make_task("t2", status=TaskStatus.DONE, assignee_ids=["a2"])
            return [t1_done, t2_done]

        mock_manager.get_project_tasks.side_effect = fake_get_project_tasks
        mock_manager.get_task.side_effect = lambda tid: {
            "t1": _make_task("t1", status=TaskStatus.INBOX, assignee_ids=["a1"]),
            "t2": _make_task("t2", status=TaskStatus.INBOX, assignee_ids=["a2"]),
        }.get(tid)

        from pocketpaw.deep_work.models import Project, ProjectStatus

        mock_manager.get_project.return_value = Project(id="proj-1", status=ProjectStatus.EXECUTING)

        scheduler = DependencyScheduler(mock_manager, mock_executor, clock=clock)
        snapshots = await scheduler.run_tick_synchronized("proj-1")

        # Tasks should have been dispatched
        assert mock_executor.execute_task_background.call_count == 2
        # simulation_tick metadata should be stamped
        assert t1.metadata.get("simulation_tick") == 0
        assert t2.metadata.get("simulation_tick") == 0
        # Clock should have advanced at least once
        assert clock.current_tick >= 1
        assert len(snapshots) >= 1

    async def test_tick_metadata_is_stamped_on_task(self, mock_manager, mock_executor, clock):
        """Tasks dispatched in tick-synchronized mode get simulation_tick in metadata."""
        t1 = _make_task("t1", assignee_ids=["a1"])

        first_call = True

        async def fake_get_project_tasks(pid):
            nonlocal first_call
            if first_call:
                first_call = False
                return [t1]
            return [_make_task("t1", status=TaskStatus.DONE, assignee_ids=["a1"])]

        mock_manager.get_project_tasks.side_effect = fake_get_project_tasks
        mock_manager.get_task.return_value = _make_task(
            "t1", status=TaskStatus.INBOX, assignee_ids=["a1"]
        )
        mock_manager.get_project.return_value = MagicMock(status="executing", title="Test")

        scheduler = DependencyScheduler(mock_manager, mock_executor, clock=clock)
        await scheduler.run_tick_synchronized("proj-1")

        # save_task should have been called with the tick stamped
        mock_manager.save_task.assert_called()
        saved_task = mock_manager.save_task.call_args[0][0]
        assert saved_task.metadata["simulation_tick"] == 0


# ============================================================================
# MCTaskExecutor — simulation_tick in prompt
# ============================================================================


class TestSimulationTickInPrompt:
    async def test_prompt_includes_simulation_tick(self):
        """When task metadata has simulation_tick, it appears in the prompt."""
        from pocketpaw.mission_control.executor import MCTaskExecutor

        executor = MCTaskExecutor()

        # Use a task with no project_id to avoid project-context lookups
        task = Task(
            id="t1",
            title="Research competitors",
            priority=TaskPriority.MEDIUM,
            metadata={"simulation_tick": 3},
        )

        agent = MagicMock()
        agent.name = "Agent-1"
        agent.role = "researcher"
        agent.description = ""
        agent.specialties = []
        agent.backend = "claude_agent_sdk"

        with patch("pocketpaw.mission_control.executor.get_mission_control_manager") as mock_mgr:
            mock_mgr.return_value = AsyncMock()
            prompt = await executor._build_task_prompt(task, agent)

        assert "**Simulation Tick:** 3" in prompt

    async def test_prompt_excludes_simulation_tick_when_absent(self):
        """When task metadata has no simulation_tick, prompt has no tick line."""
        from pocketpaw.mission_control.executor import MCTaskExecutor

        executor = MCTaskExecutor()

        task = Task(
            id="t1",
            title="Research competitors",
            priority=TaskPriority.MEDIUM,
        )

        agent = MagicMock()
        agent.name = "Agent-1"
        agent.role = "researcher"
        agent.description = ""
        agent.specialties = []
        agent.backend = "claude_agent_sdk"

        with patch("pocketpaw.mission_control.executor.get_mission_control_manager") as mock_mgr:
            mock_mgr.return_value = AsyncMock()
            prompt = await executor._build_task_prompt(task, agent)

        assert "Simulation Tick" not in prompt
