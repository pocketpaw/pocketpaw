# Tests for Deep Work Human Task Router
# Created: 2026-02-12
#
# Verifies that HumanTaskRouter correctly formats and broadcasts
# notifications for human tasks, review tasks, plan readiness,
# and project completion via the MessageBus.

from unittest.mock import AsyncMock, patch

import pytest

from pocketclaw.bus.events import Channel, OutboundMessage
from pocketclaw.deep_work.human_tasks import HumanTaskRouter
from pocketclaw.deep_work.models import Project
from pocketclaw.mission_control.models import Task, TaskPriority, TaskStatus


@pytest.fixture
def router():
    return HumanTaskRouter()


@pytest.fixture
def sample_task():
    return Task(
        id="task-001",
        title="Upload brand assets to S3",
        description="Upload the logo, favicon, and banner images to the S3 bucket.",
        priority=TaskPriority.HIGH,
        tags=["design", "assets"],
        task_type="human",
        project_id="proj-001",
    )


@pytest.fixture
def sample_project():
    return Project(
        id="proj-001",
        title="Website Redesign",
        description="Complete redesign of the marketing site",
    )


def _make_mock_bus():
    """Create a mock MessageBus with broadcast_outbound captured."""
    mock_bus = AsyncMock()
    mock_bus.broadcast_outbound = AsyncMock()
    return mock_bus


# ============================================================================
# notify_human_task
# ============================================================================


async def test_notify_human_task_publishes_outbound(router, sample_task):
    """notify_human_task should broadcast an OutboundMessage with correct content and metadata."""
    mock_bus = _make_mock_bus()
    with patch("pocketclaw.bus.get_message_bus", return_value=mock_bus):
        await router.notify_human_task(sample_task)

    mock_bus.broadcast_outbound.assert_called_once()
    msg = mock_bus.broadcast_outbound.call_args[0][0]
    assert isinstance(msg, OutboundMessage)
    assert "Task needs your help" in msg.content
    assert "Upload brand assets to S3" in msg.content
    assert msg.metadata["type"] == "human_task"
    assert msg.metadata["task_id"] == "task-001"
    assert msg.metadata["project_id"] == "proj-001"


async def test_notify_human_task_no_project_id(router):
    """notify_human_task should default project_id to empty string when None."""
    task = Task(id="task-002", title="Do something", project_id=None)
    mock_bus = _make_mock_bus()
    with patch("pocketclaw.bus.get_message_bus", return_value=mock_bus):
        await router.notify_human_task(task)

    msg = mock_bus.broadcast_outbound.call_args[0][0]
    assert msg.metadata["project_id"] == ""


# ============================================================================
# notify_review_task
# ============================================================================


async def test_notify_review_task_publishes_correct_message(router, sample_task):
    """notify_review_task should broadcast a review notification."""
    mock_bus = _make_mock_bus()
    with patch("pocketclaw.bus.get_message_bus", return_value=mock_bus):
        await router.notify_review_task(sample_task)

    mock_bus.broadcast_outbound.assert_called_once()
    msg = mock_bus.broadcast_outbound.call_args[0][0]
    assert "Task ready for review" in msg.content
    assert "Upload brand assets to S3" in msg.content
    assert "review in the dashboard" in msg.content
    assert msg.metadata["type"] == "review_task"
    assert msg.metadata["task_id"] == "task-001"


# ============================================================================
# notify_plan_ready
# ============================================================================


async def test_notify_plan_ready_includes_project_and_counts(router, sample_project):
    """notify_plan_ready should include project title, task count, and estimate."""
    mock_bus = _make_mock_bus()
    with patch("pocketclaw.bus.get_message_bus", return_value=mock_bus):
        await router.notify_plan_ready(sample_project, task_count=5, estimated_minutes=120)

    msg = mock_bus.broadcast_outbound.call_args[0][0]
    assert "Website Redesign" in msg.content
    assert "Tasks: 5" in msg.content
    assert "~120 minutes" in msg.content
    assert msg.metadata["type"] == "plan_ready"
    assert msg.metadata["project_id"] == "proj-001"


async def test_notify_plan_ready_defaults(router, sample_project):
    """notify_plan_ready should work with default task_count and estimated_minutes."""
    mock_bus = _make_mock_bus()
    with patch("pocketclaw.bus.get_message_bus", return_value=mock_bus):
        await router.notify_plan_ready(sample_project)

    msg = mock_bus.broadcast_outbound.call_args[0][0]
    assert "Tasks: 0" in msg.content
    assert "~0 minutes" in msg.content


# ============================================================================
# notify_project_completed
# ============================================================================


async def test_notify_project_completed_includes_counts(router, sample_project):
    """notify_project_completed should count done tasks correctly."""
    tasks = [
        Task(id="t1", title="A", status=TaskStatus.DONE),
        Task(id="t2", title="B", status=TaskStatus.DONE),
        Task(id="t3", title="C", status=TaskStatus.IN_PROGRESS),
    ]
    mock_bus = _make_mock_bus()
    with patch("pocketclaw.bus.get_message_bus", return_value=mock_bus):
        await router.notify_project_completed(sample_project, tasks=tasks)

    msg = mock_bus.broadcast_outbound.call_args[0][0]
    assert "Website Redesign" in msg.content
    assert "Tasks completed: 2/3" in msg.content
    assert msg.metadata["type"] == "project_completed"
    assert msg.metadata["project_id"] == "proj-001"


async def test_notify_project_completed_no_tasks(router, sample_project):
    """notify_project_completed should handle None tasks gracefully."""
    mock_bus = _make_mock_bus()
    with patch("pocketclaw.bus.get_message_bus", return_value=mock_bus):
        await router.notify_project_completed(sample_project, tasks=None)

    msg = mock_bus.broadcast_outbound.call_args[0][0]
    assert "Tasks completed: 0/0" in msg.content


# ============================================================================
# _format_task_notification
# ============================================================================


async def test_format_task_notification_all_fields(router, sample_task):
    """_format_task_notification should include title, description, priority, and tags."""
    result = router._format_task_notification(sample_task)
    assert "Task needs your help" in result
    assert "Upload brand assets to S3" in result
    assert "Upload the logo" in result
    assert "Priority: high" in result
    assert "Tags: design, assets" in result
    assert "Mark complete in the dashboard" in result


async def test_format_task_notification_truncates_long_description(router):
    """_format_task_notification should truncate descriptions over 300 chars."""
    task = Task(
        id="task-long",
        title="Long task",
        description="x" * 500,
    )
    result = router._format_task_notification(task)
    # 300 chars + "..."
    assert "..." in result
    # Should not contain the full 500-char string
    assert "x" * 500 not in result
    assert "x" * 300 in result


async def test_format_task_notification_no_description(router):
    """_format_task_notification should handle missing description."""
    task = Task(id="task-nodesc", title="Quick task", description="")
    result = router._format_task_notification(task)
    assert "Quick task" in result
    assert "Priority: medium" in result


async def test_format_task_notification_no_tags(router):
    """_format_task_notification should omit tags line when empty."""
    task = Task(id="task-notags", title="No tags task", tags=[])
    result = router._format_task_notification(task)
    assert "Tags:" not in result


# ============================================================================
# _publish_outbound error handling
# ============================================================================


async def test_publish_outbound_handles_missing_bus(router):
    """_publish_outbound should not crash if get_message_bus raises."""
    with patch(
        "pocketclaw.bus.get_message_bus",
        side_effect=RuntimeError("No event loop"),
    ):
        # Should not raise
        await router._publish_outbound("test", {"type": "test"})


async def test_publish_outbound_handles_broadcast_failure(router):
    """_publish_outbound should not crash if broadcast_outbound raises."""
    mock_bus = AsyncMock()
    mock_bus.broadcast_outbound = AsyncMock(side_effect=Exception("Network down"))
    with patch("pocketclaw.bus.get_message_bus", return_value=mock_bus):
        # Should not raise
        await router._publish_outbound("test", {"type": "test"})


async def test_publish_outbound_uses_system_channel_and_broadcast_chat_id(router):
    """_publish_outbound should use Channel.SYSTEM and chat_id='broadcast'."""
    mock_bus = _make_mock_bus()
    with patch("pocketclaw.bus.get_message_bus", return_value=mock_bus):
        await router._publish_outbound("hello", {"type": "test"})

    msg = mock_bus.broadcast_outbound.call_args[0][0]
    assert msg.channel == Channel.SYSTEM
    assert msg.chat_id == "broadcast"
