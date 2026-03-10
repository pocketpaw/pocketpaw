"""Test that MCTaskExecutor correctly handles AgentEvent dataclass objects.

Regression test for PR #482: executor was calling .get() on AgentEvent
objects (dataclass with .type/.content/.metadata attributes), causing
'AgentEvent object has no attribute get' errors.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.mission_control.executor import MCTaskExecutor, reset_mc_task_executor
from pocketpaw.mission_control.models import AgentStatus, TaskPriority, TaskStatus


# Fake agent and task objects returned by the mocked manager
def _make_fake_agent(agent_id="aaaaaaaa-1111-2222-3333-444444444444"):
    agent = MagicMock()
    agent.id = agent_id
    agent.name = "TestAgent"
    agent.role = "tester"
    agent.description = "A test agent"
    agent.specialties = ["testing"]
    agent.backend = "claude_agent_sdk"
    agent.status = AgentStatus.IDLE
    return agent


def _make_fake_task(task_id="bbbbbbbb-1111-2222-3333-444444444444"):
    task = MagicMock()
    task.id = task_id
    task.title = "Test Task"
    task.description = "Do something"
    task.priority = TaskPriority.MEDIUM
    task.status = TaskStatus.INBOX
    task.project_id = None
    task.blocked_by = []
    task.assignee_ids = []
    task.timeout_minutes = None
    task.retry_count = 0
    task.max_retries = 0
    task.output = None
    task.error_message = None
    return task


async def _fake_router_run(prompt):
    """Simulate router.run() yielding real AgentEvent dataclass objects."""
    yield AgentEvent(type="message", content="Hello, working on it...")
    yield AgentEvent(type="tool_use", content="", metadata={"name": "bash", "input": "ls"})
    yield AgentEvent(type="tool_result", content="file1.py\nfile2.py")
    yield AgentEvent(type="message", content="Done! Found 2 files.")
    yield AgentEvent(type="done", content="")


@pytest.fixture(autouse=True)
def _reset_executor():
    reset_mc_task_executor()
    yield
    reset_mc_task_executor()


@pytest.mark.asyncio
async def test_executor_handles_agent_event_dataclass():
    """The executor must handle AgentEvent dataclass objects without AttributeError.

    Before the fix, chunk.get("type") would raise:
        AttributeError: 'AgentEvent' object has no attribute 'get'
    """
    task_id = "bbbbbbbb-1111-2222-3333-444444444444"
    agent_id = "aaaaaaaa-1111-2222-3333-444444444444"
    fake_task = _make_fake_task(task_id)
    fake_agent = _make_fake_agent(agent_id)

    mock_manager = AsyncMock()
    mock_manager.get_task.return_value = fake_task
    mock_manager.get_agent.return_value = fake_agent
    mock_manager.update_task_status.return_value = True
    mock_manager.set_agent_status.return_value = True
    mock_manager.save_activity.return_value = None
    mock_manager.save_document.return_value = None
    mock_manager.get_task_documents.return_value = []

    mock_router = MagicMock()
    mock_router.run = _fake_router_run
    mock_router.stop = AsyncMock()

    mock_bus = AsyncMock()
    mock_bus.publish_system = AsyncMock()

    with (
        patch(
            "pocketpaw.mission_control.executor.get_mission_control_manager",
            return_value=mock_manager,
        ),
        patch("pocketpaw.mission_control.executor.get_message_bus", return_value=mock_bus),
        patch("pocketpaw.mission_control.executor.AgentRouter", return_value=mock_router),
        patch("pocketpaw.mission_control.executor.get_settings", return_value=MagicMock()),
    ):
        executor = MCTaskExecutor()
        result = await executor.execute_task(task_id, agent_id)

    assert result["status"] == "completed", f"Expected completed, got: {result}"
    assert "Hello, working on it..." in result["output"]
    assert "Done! Found 2 files." in result["output"]
    assert result["error"] is None


@pytest.mark.asyncio
async def test_executor_handles_tool_use_metadata():
    """Verify tool_use events extract tool name from metadata attribute."""
    task_id = "bbbbbbbb-1111-2222-3333-444444444444"
    agent_id = "aaaaaaaa-1111-2222-3333-444444444444"

    async def router_with_tool(prompt):
        yield AgentEvent(
            type="tool_use", content="", metadata={"name": "read_file", "path": "/tmp/x"}
        )
        yield AgentEvent(type="tool_result", content="file contents here")
        yield AgentEvent(type="done", content="")

    mock_manager = AsyncMock()
    mock_manager.get_task.return_value = _make_fake_task(task_id)
    mock_manager.get_agent.return_value = _make_fake_agent(agent_id)
    mock_manager.update_task_status.return_value = True
    mock_manager.set_agent_status.return_value = True
    mock_manager.save_activity.return_value = None

    mock_router = MagicMock()
    mock_router.run = router_with_tool
    mock_router.stop = AsyncMock()

    mock_bus = AsyncMock()
    mock_bus.publish_system = AsyncMock()

    broadcast_events = []

    async def capture_event(event):
        broadcast_events.append(event)

    mock_bus.publish_system = capture_event

    with (
        patch(
            "pocketpaw.mission_control.executor.get_mission_control_manager",
            return_value=mock_manager,
        ),
        patch("pocketpaw.mission_control.executor.get_message_bus", return_value=mock_bus),
        patch("pocketpaw.mission_control.executor.AgentRouter", return_value=mock_router),
        patch("pocketpaw.mission_control.executor.get_settings", return_value=MagicMock()),
    ):
        executor = MCTaskExecutor()
        result = await executor.execute_task(task_id, agent_id)

    assert result["status"] == "completed"

    # Find the tool_use broadcast event
    tool_events = [
        e
        for e in broadcast_events
        if e.event_type == "mc_task_output" and e.data.get("output_type") == "tool_use"
    ]
    assert len(tool_events) == 1
    assert "read_file" in tool_events[0].data["content"]


@pytest.mark.asyncio
async def test_executor_handles_error_event():
    """Verify error events from AgentEvent are handled correctly."""
    task_id = "bbbbbbbb-1111-2222-3333-444444444444"
    agent_id = "aaaaaaaa-1111-2222-3333-444444444444"

    async def router_with_error(prompt):
        yield AgentEvent(type="message", content="Starting...")
        yield AgentEvent(type="error", content="Something went wrong")

    mock_manager = AsyncMock()
    mock_manager.get_task.return_value = _make_fake_task(task_id)
    mock_manager.get_agent.return_value = _make_fake_agent(agent_id)
    mock_manager.update_task_status.return_value = True
    mock_manager.set_agent_status.return_value = True
    mock_manager.save_activity.return_value = None

    mock_router = MagicMock()
    mock_router.run = router_with_error
    mock_router.stop = AsyncMock()

    mock_bus = AsyncMock()
    mock_bus.publish_system = AsyncMock()

    with (
        patch(
            "pocketpaw.mission_control.executor.get_mission_control_manager",
            return_value=mock_manager,
        ),
        patch("pocketpaw.mission_control.executor.get_message_bus", return_value=mock_bus),
        patch("pocketpaw.mission_control.executor.AgentRouter", return_value=mock_router),
        patch("pocketpaw.mission_control.executor.get_settings", return_value=MagicMock()),
    ):
        executor = MCTaskExecutor()
        result = await executor.execute_task(task_id, agent_id)

    assert result["status"] == "error"
    assert result["error"] == "Something went wrong"
