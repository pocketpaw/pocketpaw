"""Mission Control - Multi-agent orchestration for PocketPaw.

Created: 2026-02-05
Updated: 2026-02-05 - Added MCTaskExecutor for agent task execution with WebSocket streaming

Mission Control provides a shared workspace where multiple AI agents
can work together like a team. Features:

- Agent profiles with roles, status, and capabilities
- Task management with lifecycle (inbox -> assigned -> in_progress -> review -> done)
- Message threads for discussions on tasks
- Activity feed for real-time visibility
- Document storage for deliverables
- Notification system with @mentions
- Heartbeat system for agent status tracking
- Task execution with real-time streaming via WebSocket

Usage:
    from pocketpaw.mission_control import get_mission_control_manager

    manager = get_mission_control_manager()

    # Create an agent
    agent = await manager.create_agent(
        name="Jarvis",
        role="Squad Lead",
        description="Coordinates the team and handles requests"
    )

    # Create a task
    task = await manager.create_task(
        title="Research competitors",
        description="Analyze top 5 competitors",
        assignee_ids=[agent.id]
    )

    # Post a message
    await manager.post_message(
        task_id=task.id,
        from_agent_id=agent.id,
        content="Starting research now. @all please share any insights."
    )

    # Execute a task with an agent
    from pocketpaw.mission_control import get_mc_task_executor
    executor = get_mc_task_executor()
    await executor.execute_task(task.id, agent.id)

    # Get activity feed
    activities = await manager.get_activity_feed()
"""

# Models
# Manager
# API
from pocketpaw.mission_control.api import router as mission_control_router

# Executor
from pocketpaw.mission_control.executor import (
    MCTaskExecutor,
    get_mc_task_executor,
    reset_mc_task_executor,
)

# Heartbeat
from pocketpaw.mission_control.heartbeat import (
    HeartbeatDaemon,
    get_heartbeat_daemon,
    reset_heartbeat_daemon,
)
from pocketpaw.mission_control.manager import (
    MissionControlManager,
    get_mission_control_manager,
    reset_mission_control_manager,
)
from pocketpaw.mission_control.models import (
    Activity,
    ActivityType,
    AgentLevel,
    AgentProfile,
    AgentStatus,
    Document,
    DocumentType,
    Message,
    Notification,
    Task,
    TaskPriority,
    TaskStatus,
)

# Store
from pocketpaw.mission_control.store import (
    FileMissionControlStore,
    get_mission_control_store,
    reset_mission_control_store,
)

__all__ = [
    # Models
    "AgentProfile",
    "AgentStatus",
    "AgentLevel",
    "Task",
    "TaskStatus",
    "TaskPriority",
    "Message",
    "Activity",
    "ActivityType",
    "Document",
    "DocumentType",
    "Notification",
    # Store
    "FileMissionControlStore",
    "get_mission_control_store",
    "reset_mission_control_store",
    # Manager
    "MissionControlManager",
    "get_mission_control_manager",
    "reset_mission_control_manager",
    # API
    "mission_control_router",
    # Executor
    "MCTaskExecutor",
    "get_mc_task_executor",
    "reset_mc_task_executor",
    # Heartbeat
    "HeartbeatDaemon",
    "get_heartbeat_daemon",
    "reset_heartbeat_daemon",
]
