# Tests for Mission Control Heartbeat System
# Created: 2026-02-05
# Tests the background daemon for agent heartbeats

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pocketpaw.mission_control import (
    FileMissionControlStore,
    MissionControlManager,
    reset_mission_control_manager,
    reset_mission_control_store,
)
from pocketpaw.mission_control.heartbeat import (
    HeartbeatDaemon,
    reset_heartbeat_daemon,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_store_path():
    """Create a temporary directory for test storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def store(temp_store_path):
    """Create a fresh store for each test."""
    reset_mission_control_store()
    return FileMissionControlStore(temp_store_path)


@pytest.fixture
def manager(store):
    """Create a manager with the test store."""
    reset_mission_control_manager()
    return MissionControlManager(store)


@pytest.fixture
def daemon():
    """Create a fresh heartbeat daemon for each test."""
    reset_heartbeat_daemon()
    d = HeartbeatDaemon(interval_minutes=1)  # Short interval for testing
    yield d
    d.stop()


@pytest.fixture
def patched_daemon(store, manager, monkeypatch):
    """Create daemon with patched manager."""
    reset_heartbeat_daemon()

    # Patch the get functions
    import pocketpaw.mission_control.manager as manager_module
    import pocketpaw.mission_control.store as store_module

    monkeypatch.setattr(store_module, "_store_instance", store)
    monkeypatch.setattr(manager_module, "_manager_instance", manager)

    d = HeartbeatDaemon(interval_minutes=1)
    yield d
    d.stop()


# ============================================================================
# Basic Daemon Tests
# ============================================================================


class TestHeartbeatDaemon:
    """Tests for HeartbeatDaemon."""

    def test_init_default_interval(self):
        """Test daemon initializes with default interval."""
        d = HeartbeatDaemon()
        assert d._interval_minutes == 15
        d.stop()

    def test_init_custom_interval(self):
        """Test daemon initializes with custom interval."""
        d = HeartbeatDaemon(interval_minutes=5)
        assert d._interval_minutes == 5
        d.stop()

    @pytest.mark.asyncio
    async def test_start_stop(self, daemon):
        """Test daemon can start and stop."""
        daemon.start()
        assert daemon._running is True

        daemon.stop()
        assert daemon._running is False

    @pytest.mark.asyncio
    async def test_start_twice_warns(self, daemon, caplog):
        """Test starting twice logs a warning."""
        daemon.start()
        daemon.start()  # Second start should warn

        assert "already running" in caplog.text.lower()
        daemon.stop()

    @pytest.mark.asyncio
    async def test_set_interval(self, daemon):
        """Test changing heartbeat interval."""
        daemon.start()
        daemon.set_interval(10)
        assert daemon._interval_minutes == 10
        daemon.stop()


# ============================================================================
# Wake Agent Tests
# ============================================================================


class TestWakeAgent:
    """Tests for waking agents and checking work."""

    @pytest.mark.asyncio
    async def test_wake_agent_records_heartbeat(self, patched_daemon, manager):
        """Test waking an agent records their heartbeat."""
        # Create agent
        agent = await manager.create_agent(name="TestAgent", role="Test")
        assert agent.last_heartbeat is None

        # Wake agent
        await patched_daemon._wake_agent(agent.id)

        # Check heartbeat recorded
        updated = await manager.get_agent(agent.id)
        assert updated.last_heartbeat is not None

    @pytest.mark.asyncio
    async def test_wake_agent_with_callback(self, patched_daemon, manager):
        """Test waking an agent fires callback."""
        callback = AsyncMock()
        patched_daemon._callback = callback

        agent = await manager.create_agent(name="CallbackAgent", role="Test")
        await patched_daemon._wake_agent(agent.id)

        callback.assert_called_once()
        call_args = callback.call_args
        assert call_args[0][0] == agent.id  # First arg is agent_id
        assert "agent_name" in call_args[0][1]  # Second arg is event data

    @pytest.mark.asyncio
    async def test_check_for_work_no_work(self, patched_daemon, manager):
        """Test checking for work when there's none."""
        agent = await manager.create_agent(name="IdleAgent", role="Test")

        work = await patched_daemon._check_for_work(agent.id)

        assert work["has_work"] is False
        assert work["has_urgent_work"] is False
        assert work["unread_notifications"] == 0
        assert work["assigned_tasks"] == 0

    @pytest.mark.asyncio
    async def test_check_for_work_with_tasks(self, patched_daemon, manager):
        """Test checking for work when tasks are assigned."""
        agent = await manager.create_agent(name="BusyAgent", role="Test")

        # Assign a task
        await manager.create_task(
            title="Test Task",
            assignee_ids=[agent.id],
        )

        work = await patched_daemon._check_for_work(agent.id)

        assert work["has_work"] is True
        assert work["assigned_tasks"] == 1

    @pytest.mark.asyncio
    async def test_check_for_work_with_notifications(self, patched_daemon, manager):
        """Test checking for work when there are notifications."""
        sender = await manager.create_agent(name="Sender", role="Test")
        target = await manager.create_agent(name="Target", role="Test")

        # Create task and post message with @mention
        task = await manager.create_task(title="Mention Task")
        await manager.post_message(
            task_id=task.id,
            from_agent_id=sender.id,
            content="Hey @Target, check this!",
        )

        # Check target's work
        work = await patched_daemon._check_for_work(target.id)

        assert work["has_work"] is True
        assert work["has_urgent_work"] is True
        assert work["unread_notifications"] > 0


# ============================================================================
# Heartbeat Cycle Tests
# ============================================================================


class TestHeartbeatCycle:
    """Tests for the full heartbeat cycle."""

    @pytest.mark.asyncio
    async def test_cycle_wakes_all_agents(self, patched_daemon, manager):
        """Test heartbeat cycle wakes all agents."""
        # Create multiple agents
        agent1 = await manager.create_agent(name="Agent1", role="Role1")
        agent2 = await manager.create_agent(name="Agent2", role="Role2")
        agent3 = await manager.create_agent(name="Agent3", role="Role3")

        # Run cycle
        patched_daemon._running = True
        await patched_daemon._heartbeat_cycle()

        # All agents should have heartbeats
        for agent_id in [agent1.id, agent2.id, agent3.id]:
            agent = await manager.get_agent(agent_id)
            assert agent.last_heartbeat is not None

    @pytest.mark.asyncio
    async def test_cycle_stops_when_not_running(self, patched_daemon, manager):
        """Test heartbeat cycle respects running flag."""
        # Create agents
        await manager.create_agent(name="Agent1", role="Role1")
        await manager.create_agent(name="Agent2", role="Role2")

        # Start cycle but immediately stop
        patched_daemon._running = True

        # Mock to stop after first agent
        original_wake = patched_daemon._wake_agent
        call_count = 0

        async def stop_after_one(agent_id):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                patched_daemon._running = False
            await original_wake(agent_id)

        patched_daemon._wake_agent = stop_after_one
        await patched_daemon._heartbeat_cycle()

        # Should have only woken one agent before stopping
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_cycle_handles_errors(self, patched_daemon, manager, caplog):
        """Test heartbeat cycle handles errors gracefully."""
        # Create agent
        await manager.create_agent(name="ErrorAgent", role="Test")

        # Mock wake_agent to raise error
        async def raise_error(agent_id):
            raise RuntimeError("Test error")

        patched_daemon._wake_agent = raise_error
        patched_daemon._running = True

        # Should not raise
        await patched_daemon._heartbeat_cycle()

        # Error should be logged
        assert "error" in caplog.text.lower()


# ============================================================================
# Manual Trigger Tests
# ============================================================================


class TestManualTrigger:
    """Tests for manual heartbeat triggers."""

    @pytest.mark.asyncio
    async def test_trigger_heartbeat(self, patched_daemon, manager):
        """Test manually triggering a heartbeat."""
        agent = await manager.create_agent(name="ManualAgent", role="Test")

        # Assign task
        await manager.create_task(
            title="Manual Task",
            assignee_ids=[agent.id],
        )

        # Trigger heartbeat
        work = await patched_daemon.trigger_heartbeat(agent.id)

        assert work["has_work"] is True
        assert work["assigned_tasks"] == 1

        # Agent should have heartbeat recorded
        updated = await manager.get_agent(agent.id)
        assert updated.last_heartbeat is not None
