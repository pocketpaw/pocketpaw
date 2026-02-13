"""Mission Control storage protocol.

Created: 2026-02-05
Updated: 2026-02-12 — Added Project method signatures for Deep Work orchestration.

Defines the interface for Mission Control storage backends.

Following PocketPaw's protocol-first design pattern (like MemoryStoreProtocol),
this allows for swappable storage implementations:
- FileStore: JSON files (default, simple)
- Future: SQLite, PostgreSQL, Convex, etc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pocketclaw.deep_work.models import Project

from pocketclaw.mission_control.models import (
    Activity,
    AgentProfile,
    Document,
    Message,
    Notification,
    Task,
    TaskStatus,
)


@runtime_checkable
class MissionControlStoreProtocol(Protocol):
    """Protocol defining the interface for Mission Control storage.

    All storage backends must implement these methods.
    The protocol is divided into sections for each entity type.
    """

    # =========================================================================
    # Project Operations
    # =========================================================================

    async def save_project(self, project: Project) -> str:
        """Save or update a project.

        Returns the project ID.
        """
        ...

    async def get_project(self, project_id: str) -> Project | None:
        """Get a project by ID."""
        ...

    async def list_projects(
        self,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Project]:
        """List projects, optionally filtered by status."""
        ...

    async def delete_project(self, project_id: str) -> bool:
        """Delete a project. Returns True if deleted."""
        ...

    # =========================================================================
    # Agent Operations
    # =========================================================================

    async def save_agent(self, agent: AgentProfile) -> str:
        """Save or update an agent profile.

        Returns the agent ID.
        """
        ...

    async def get_agent(self, agent_id: str) -> AgentProfile | None:
        """Get an agent by ID."""
        ...

    async def get_agent_by_name(self, name: str) -> AgentProfile | None:
        """Get an agent by name (case-insensitive)."""
        ...

    async def get_agent_by_session_key(self, session_key: str) -> AgentProfile | None:
        """Get an agent by their session key."""
        ...

    async def list_agents(self, status: str | None = None, limit: int = 100) -> list[AgentProfile]:
        """List agents, optionally filtered by status."""
        ...

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete an agent. Returns True if deleted."""
        ...

    async def update_agent_heartbeat(self, agent_id: str) -> bool:
        """Update an agent's last_heartbeat to now. Returns True if successful."""
        ...

    # =========================================================================
    # Task Operations
    # =========================================================================

    async def save_task(self, task: Task) -> str:
        """Save or update a task.

        Returns the task ID.
        """
        ...

    async def get_task(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        ...

    async def list_tasks(
        self,
        status: TaskStatus | None = None,
        assignee_id: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
    ) -> list[Task]:
        """List tasks with optional filters."""
        ...

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task. Returns True if deleted."""
        ...

    async def get_tasks_for_agent(self, agent_id: str) -> list[Task]:
        """Get all tasks assigned to an agent."""
        ...

    async def get_blocked_tasks(self) -> list[Task]:
        """Get all tasks with BLOCKED status."""
        ...

    # =========================================================================
    # Message Operations
    # =========================================================================

    async def save_message(self, message: Message) -> str:
        """Save a message.

        Returns the message ID.
        """
        ...

    async def get_message(self, message_id: str) -> Message | None:
        """Get a message by ID."""
        ...

    async def get_messages_for_task(self, task_id: str, limit: int = 100) -> list[Message]:
        """Get all messages for a task, ordered by created_at."""
        ...

    async def delete_message(self, message_id: str) -> bool:
        """Delete a message. Returns True if deleted."""
        ...

    # =========================================================================
    # Activity Operations
    # =========================================================================

    async def save_activity(self, activity: Activity) -> str:
        """Save an activity entry.

        Returns the activity ID.
        """
        ...

    async def get_activities(
        self,
        agent_id: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[Activity]:
        """Get recent activities, optionally filtered."""
        ...

    async def get_activity_feed(self, limit: int = 50) -> list[Activity]:
        """Get the activity feed (most recent first)."""
        ...

    # =========================================================================
    # Document Operations
    # =========================================================================

    async def save_document(self, document: Document) -> str:
        """Save or update a document.

        Returns the document ID.
        """
        ...

    async def get_document(self, document_id: str) -> Document | None:
        """Get a document by ID."""
        ...

    async def list_documents(
        self,
        type: str | None = None,
        task_id: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
    ) -> list[Document]:
        """List documents with optional filters."""
        ...

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document. Returns True if deleted."""
        ...

    # =========================================================================
    # Notification Operations
    # =========================================================================

    async def save_notification(self, notification: Notification) -> str:
        """Save a notification.

        Returns the notification ID.
        """
        ...

    async def get_notification(self, notification_id: str) -> Notification | None:
        """Get a notification by ID."""
        ...

    async def get_undelivered_notifications(
        self, agent_id: str | None = None
    ) -> list[Notification]:
        """Get notifications that haven't been delivered yet."""
        ...

    async def get_notifications_for_agent(
        self, agent_id: str, unread_only: bool = False, limit: int = 50
    ) -> list[Notification]:
        """Get notifications for a specific agent."""
        ...

    async def mark_notification_delivered(self, notification_id: str) -> bool:
        """Mark a notification as delivered."""
        ...

    async def mark_notification_read(self, notification_id: str) -> bool:
        """Mark a notification as read."""
        ...

    async def delete_notification(self, notification_id: str) -> bool:
        """Delete a notification. Returns True if deleted."""
        ...

    # =========================================================================
    # Utility Operations
    # =========================================================================

    async def get_stats(self) -> dict[str, Any]:
        """Get statistics about the Mission Control state.

        Returns counts of agents, tasks by status, messages, etc.
        """
        ...

    async def clear_all(self) -> None:
        """Clear all data. Use with caution!"""
        ...
