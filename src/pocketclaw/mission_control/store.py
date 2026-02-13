"""File-based Mission Control store.

Created: 2026-02-05
Updated: 2026-02-12 — Added Project entity for Deep Work orchestration layer.

Implements MissionControlStoreProtocol using JSON files.

Storage layout:
~/.pocketclaw/mission_control/
    agents.json         # All agent profiles
    tasks.json          # All tasks
    messages.json       # All messages (indexed by task_id)
    activities.json     # Activity feed
    documents.json      # All documents
    notifications.json  # All notifications
    projects.json       # All Deep Work projects

Design notes:
- Single JSON file per entity type for simplicity
- In-memory index for fast lookups (like FileMemoryStore)
- Atomic writes using temp file + rename
- Suitable for personal use (< 10k records per type)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocketclaw.deep_work.models import Project

from pocketclaw.mission_control.models import (
    Activity,
    AgentProfile,
    AgentStatus,
    Document,
    Message,
    Notification,
    Task,
    TaskStatus,
    now_iso,
)

logger = logging.getLogger(__name__)


class FileMissionControlStore:
    """File-based implementation of Mission Control storage.

    Uses JSON files for persistence and maintains in-memory indexes
    for fast lookups. Suitable for personal/small team use.
    """

    def __init__(self, base_path: Path | None = None):
        """Initialize the store.

        Args:
            base_path: Directory for storage files. Defaults to ~/.pocketclaw/mission_control/
        """
        if base_path is None:
            base_path = Path.home() / ".pocketclaw" / "mission_control"

        self.base_path = base_path
        self.base_path.mkdir(parents=True, exist_ok=True)

        # File paths
        self._agents_file = self.base_path / "agents.json"
        self._tasks_file = self.base_path / "tasks.json"
        self._messages_file = self.base_path / "messages.json"
        self._activities_file = self.base_path / "activities.json"
        self._documents_file = self.base_path / "documents.json"
        self._notifications_file = self.base_path / "notifications.json"
        self._projects_file = self.base_path / "projects.json"

        # In-memory indexes
        self._agents: dict[str, AgentProfile] = {}
        self._tasks: dict[str, Task] = {}
        self._messages: dict[str, Message] = {}
        self._activities: dict[str, Activity] = {}
        self._documents: dict[str, Document] = {}
        self._notifications: dict[str, Notification] = {}
        self._projects: dict[str, Project] = {}

        # Load existing data
        self._load_all()

    # =========================================================================
    # File I/O Helpers
    # =========================================================================

    def _load_json(self, path: Path) -> list[dict[str, Any]]:
        """Load a JSON file, returning empty list if not found."""
        if not path.exists():
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Error loading {path}: {e}")
            return []

    def _save_json(self, path: Path, data: list[dict[str, Any]]) -> None:
        """Save data to JSON file atomically."""
        temp_path = path.with_suffix(".tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            temp_path.replace(path)
        except OSError as e:
            logger.error(f"Error saving {path}: {e}")
            if temp_path.exists():
                temp_path.unlink()

    def _load_all(self) -> None:
        """Load all data from files into memory."""
        # Load agents
        for data in self._load_json(self._agents_file):
            agent = AgentProfile.from_dict(data)
            self._agents[agent.id] = agent

        # Load tasks
        for data in self._load_json(self._tasks_file):
            task = Task.from_dict(data)
            self._tasks[task.id] = task

        # Load messages
        for data in self._load_json(self._messages_file):
            message = Message.from_dict(data)
            self._messages[message.id] = message

        # Load activities
        for data in self._load_json(self._activities_file):
            activity = Activity.from_dict(data)
            self._activities[activity.id] = activity

        # Load documents
        for data in self._load_json(self._documents_file):
            document = Document.from_dict(data)
            self._documents[document.id] = document

        # Load notifications
        for data in self._load_json(self._notifications_file):
            notification = Notification.from_dict(data)
            self._notifications[notification.id] = notification

        # Load projects (lazy import to avoid circular dependency)
        from pocketclaw.deep_work.models import Project as _Project

        for data in self._load_json(self._projects_file):
            project = _Project.from_dict(data)
            self._projects[project.id] = project

        logger.info(
            f"Mission Control loaded: {len(self._agents)} agents, "
            f"{len(self._tasks)} tasks, {len(self._messages)} messages, "
            f"{len(self._projects)} projects"
        )

    def _persist_agents(self) -> None:
        """Persist agents to file."""
        data = [a.to_dict() for a in self._agents.values()]
        self._save_json(self._agents_file, data)

    def _persist_tasks(self) -> None:
        """Persist tasks to file."""
        data = [t.to_dict() for t in self._tasks.values()]
        self._save_json(self._tasks_file, data)

    def _persist_messages(self) -> None:
        """Persist messages to file."""
        data = [m.to_dict() for m in self._messages.values()]
        self._save_json(self._messages_file, data)

    def _persist_activities(self) -> None:
        """Persist activities to file."""
        data = [a.to_dict() for a in self._activities.values()]
        self._save_json(self._activities_file, data)

    def _persist_documents(self) -> None:
        """Persist documents to file."""
        data = [d.to_dict() for d in self._documents.values()]
        self._save_json(self._documents_file, data)

    def _persist_notifications(self) -> None:
        """Persist notifications to file."""
        data = [n.to_dict() for n in self._notifications.values()]
        self._save_json(self._notifications_file, data)

    def _persist_projects(self) -> None:
        """Persist projects to file."""
        data = [p.to_dict() for p in self._projects.values()]
        self._save_json(self._projects_file, data)

    # =========================================================================
    # Agent Operations
    # =========================================================================

    async def save_agent(self, agent: AgentProfile) -> str:
        """Save or update an agent profile."""
        agent.updated_at = now_iso()
        self._agents[agent.id] = agent
        self._persist_agents()
        return agent.id

    async def get_agent(self, agent_id: str) -> AgentProfile | None:
        """Get an agent by ID."""
        return self._agents.get(agent_id)

    async def get_agent_by_name(self, name: str) -> AgentProfile | None:
        """Get an agent by name (case-insensitive)."""
        name_lower = name.lower()
        for agent in self._agents.values():
            if agent.name.lower() == name_lower:
                return agent
        return None

    async def get_agent_by_session_key(self, session_key: str) -> AgentProfile | None:
        """Get an agent by their session key."""
        for agent in self._agents.values():
            if agent.session_key == session_key:
                return agent
        return None

    async def list_agents(self, status: str | None = None, limit: int = 100) -> list[AgentProfile]:
        """List agents, optionally filtered by status."""
        agents = list(self._agents.values())
        if status:
            agents = [a for a in agents if a.status.value == status]
        # Sort by name
        agents.sort(key=lambda a: a.name.lower())
        return agents[:limit]

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete an agent."""
        if agent_id in self._agents:
            del self._agents[agent_id]
            self._persist_agents()
            return True
        return False

    async def update_agent_heartbeat(self, agent_id: str) -> bool:
        """Update an agent's last_heartbeat to now."""
        agent = self._agents.get(agent_id)
        if agent:
            agent.last_heartbeat = now_iso()
            agent.status = AgentStatus.IDLE  # Reset to idle after heartbeat
            self._persist_agents()
            return True
        return False

    # =========================================================================
    # Task Operations
    # =========================================================================

    async def save_task(self, task: Task) -> str:
        """Save or update a task."""
        task.updated_at = now_iso()
        self._tasks[task.id] = task
        self._persist_tasks()
        return task.id

    async def get_task(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        return self._tasks.get(task_id)

    async def list_tasks(
        self,
        status: TaskStatus | None = None,
        assignee_id: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
    ) -> list[Task]:
        """List tasks with optional filters.

        Args:
            limit: Max results. 0 means no limit.
        """
        tasks = list(self._tasks.values())

        if status:
            tasks = [t for t in tasks if t.status == status]

        if assignee_id:
            tasks = [t for t in tasks if assignee_id in t.assignee_ids]

        if tags:
            tasks = [t for t in tasks if any(tag in t.tags for tag in tags)]

        # Sort by updated_at (most recent first)
        tasks.sort(key=lambda t: t.updated_at, reverse=True)
        return tasks[:limit] if limit else tasks

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task."""
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._persist_tasks()
            return True
        return False

    async def get_tasks_for_agent(self, agent_id: str) -> list[Task]:
        """Get all tasks assigned to an agent."""
        tasks = [t for t in self._tasks.values() if agent_id in t.assignee_ids]
        tasks.sort(key=lambda t: t.updated_at, reverse=True)
        return tasks

    async def get_blocked_tasks(self) -> list[Task]:
        """Get all tasks with BLOCKED status."""
        return [t for t in self._tasks.values() if t.status == TaskStatus.BLOCKED]

    # =========================================================================
    # Message Operations
    # =========================================================================

    async def save_message(self, message: Message) -> str:
        """Save a message."""
        self._messages[message.id] = message
        self._persist_messages()
        return message.id

    async def get_message(self, message_id: str) -> Message | None:
        """Get a message by ID."""
        return self._messages.get(message_id)

    async def get_messages_for_task(self, task_id: str, limit: int = 100) -> list[Message]:
        """Get all messages for a task, ordered by created_at."""
        messages = [m for m in self._messages.values() if m.task_id == task_id]
        messages.sort(key=lambda m: m.created_at)
        return messages[:limit]

    async def delete_message(self, message_id: str) -> bool:
        """Delete a message."""
        if message_id in self._messages:
            del self._messages[message_id]
            self._persist_messages()
            return True
        return False

    # =========================================================================
    # Activity Operations
    # =========================================================================

    async def save_activity(self, activity: Activity) -> str:
        """Save an activity entry."""
        self._activities[activity.id] = activity
        self._persist_activities()
        return activity.id

    async def get_activities(
        self,
        agent_id: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[Activity]:
        """Get recent activities, optionally filtered."""
        activities = list(self._activities.values())

        if agent_id:
            activities = [a for a in activities if a.agent_id == agent_id]

        if task_id:
            activities = [a for a in activities if a.task_id == task_id]

        # Sort by created_at (most recent first)
        activities.sort(key=lambda a: a.created_at, reverse=True)
        return activities[:limit]

    async def get_activity_feed(self, limit: int = 50) -> list[Activity]:
        """Get the activity feed (most recent first)."""
        activities = list(self._activities.values())
        activities.sort(key=lambda a: a.created_at, reverse=True)
        return activities[:limit]

    # =========================================================================
    # Document Operations
    # =========================================================================

    async def save_document(self, document: Document) -> str:
        """Save or update a document."""
        # Increment version if updating existing doc
        existing = self._documents.get(document.id)
        if existing:
            document.version = existing.version + 1
        document.updated_at = now_iso()
        self._documents[document.id] = document
        self._persist_documents()
        return document.id

    async def get_document(self, document_id: str) -> Document | None:
        """Get a document by ID."""
        return self._documents.get(document_id)

    async def list_documents(
        self,
        type: str | None = None,
        task_id: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
    ) -> list[Document]:
        """List documents with optional filters."""
        documents = list(self._documents.values())

        if type:
            documents = [d for d in documents if d.type.value == type]

        if task_id:
            documents = [d for d in documents if d.task_id == task_id]

        if tags:
            documents = [d for d in documents if any(tag in d.tags for tag in tags)]

        # Sort by updated_at (most recent first)
        documents.sort(key=lambda d: d.updated_at, reverse=True)
        return documents[:limit]

    async def delete_document(self, document_id: str) -> bool:
        """Delete a document."""
        if document_id in self._documents:
            del self._documents[document_id]
            self._persist_documents()
            return True
        return False

    # =========================================================================
    # Notification Operations
    # =========================================================================

    async def save_notification(self, notification: Notification) -> str:
        """Save a notification."""
        self._notifications[notification.id] = notification
        self._persist_notifications()
        return notification.id

    async def get_notification(self, notification_id: str) -> Notification | None:
        """Get a notification by ID."""
        return self._notifications.get(notification_id)

    async def get_undelivered_notifications(
        self, agent_id: str | None = None
    ) -> list[Notification]:
        """Get notifications that haven't been delivered yet."""
        notifications = [n for n in self._notifications.values() if not n.delivered]
        if agent_id:
            notifications = [n for n in notifications if n.agent_id == agent_id]
        notifications.sort(key=lambda n: n.created_at)
        return notifications

    async def get_notifications_for_agent(
        self, agent_id: str, unread_only: bool = False, limit: int = 50
    ) -> list[Notification]:
        """Get notifications for a specific agent."""
        notifications = [n for n in self._notifications.values() if n.agent_id == agent_id]
        if unread_only:
            notifications = [n for n in notifications if not n.read]
        notifications.sort(key=lambda n: n.created_at, reverse=True)
        return notifications[:limit]

    async def mark_notification_delivered(self, notification_id: str) -> bool:
        """Mark a notification as delivered."""
        notification = self._notifications.get(notification_id)
        if notification:
            notification.delivered = True
            notification.delivered_at = now_iso()
            self._persist_notifications()
            return True
        return False

    async def mark_notification_read(self, notification_id: str) -> bool:
        """Mark a notification as read."""
        notification = self._notifications.get(notification_id)
        if notification:
            notification.read = True
            self._persist_notifications()
            return True
        return False

    async def delete_notification(self, notification_id: str) -> bool:
        """Delete a notification."""
        if notification_id in self._notifications:
            del self._notifications[notification_id]
            self._persist_notifications()
            return True
        return False

    # =========================================================================
    # Project Operations
    # =========================================================================

    async def save_project(self, project: Project) -> str:
        """Save or update a project."""
        project.updated_at = now_iso()
        self._projects[project.id] = project
        self._persist_projects()
        return project.id

    async def get_project(self, project_id: str) -> Project | None:
        """Get a project by ID."""
        return self._projects.get(project_id)

    async def list_projects(
        self,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Project]:
        """List projects, optionally filtered by status."""
        projects = list(self._projects.values())
        if status:
            projects = [p for p in projects if p.status.value == status]
        # Sort by updated_at (most recent first)
        projects.sort(key=lambda p: p.updated_at, reverse=True)
        return projects[:limit]

    async def delete_project(self, project_id: str) -> bool:
        """Delete a project."""
        if project_id in self._projects:
            del self._projects[project_id]
            self._persist_projects()
            return True
        return False

    # =========================================================================
    # Utility Operations
    # =========================================================================

    async def get_stats(self) -> dict[str, Any]:
        """Get statistics about the Mission Control state."""
        task_counts = {}
        for status in TaskStatus:
            task_counts[status.value] = len([t for t in self._tasks.values() if t.status == status])

        agent_counts = {}
        for status in AgentStatus:
            agent_counts[status.value] = len(
                [a for a in self._agents.values() if a.status == status]
            )

        from pocketclaw.deep_work.models import ProjectStatus

        project_counts = {}
        for status in ProjectStatus:
            project_counts[status.value] = len(
                [p for p in self._projects.values() if p.status == status]
            )

        return {
            "agents": {
                "total": len(self._agents),
                "by_status": agent_counts,
            },
            "tasks": {
                "total": len(self._tasks),
                "by_status": task_counts,
            },
            "messages": {"total": len(self._messages)},
            "activities": {"total": len(self._activities)},
            "documents": {"total": len(self._documents)},
            "notifications": {
                "total": len(self._notifications),
                "undelivered": len([n for n in self._notifications.values() if not n.delivered]),
                "unread": len([n for n in self._notifications.values() if not n.read]),
            },
            "projects": {
                "total": len(self._projects),
                "by_status": project_counts,
            },
        }

    async def clear_all(self) -> None:
        """Clear all data. Use with caution!"""
        self._agents.clear()
        self._tasks.clear()
        self._messages.clear()
        self._activities.clear()
        self._documents.clear()
        self._notifications.clear()
        self._projects.clear()

        self._persist_agents()
        self._persist_tasks()
        self._persist_messages()
        self._persist_activities()
        self._persist_documents()
        self._persist_notifications()
        self._persist_projects()

        logger.warning("Mission Control data cleared!")


# =========================================================================
# Factory Function
# =========================================================================

_store_instance: FileMissionControlStore | None = None


def get_mission_control_store(base_path: Path | None = None) -> FileMissionControlStore:
    """Get or create the Mission Control store singleton.

    Args:
        base_path: Optional custom storage path. Only used on first call.

    Returns:
        The FileMissionControlStore instance.
    """
    global _store_instance
    if _store_instance is None:
        _store_instance = FileMissionControlStore(base_path)
    return _store_instance


def reset_mission_control_store() -> None:
    """Reset the store singleton (for testing)."""
    global _store_instance
    _store_instance = None
