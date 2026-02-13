"""Mission Control manager.

Created: 2026-02-05
Updated: 2026-02-12 — Added project directory management:
  - create_project() now creates ~/.pocketclaw/projects/{id}/ on disk
  - delete_project() now removes the project directory via shutil.rmtree()
  - Added ensure_project_directories() for startup migration
  Previous: Added skipped count to get_project_progress(), project CRUD.

High-level operations for Mission Control.

Similar to MemoryManager, this provides convenient methods
that combine storage operations with business logic:
- Creating tasks with activity logging
- Assigning tasks with notifications
- Extracting @mentions from messages
- Managing agent heartbeats
- Generating daily standups
- Project lifecycle management (Deep Work)
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pocketclaw.deep_work.models import Project

from pocketclaw.mission_control.models import (
    Activity,
    ActivityType,
    AgentProfile,
    AgentStatus,
    Document,
    DocumentType,
    Message,
    Notification,
    Task,
    TaskPriority,
    TaskStatus,
    now_iso,
)
from pocketclaw.mission_control.store import (
    FileMissionControlStore,
    get_mission_control_store,
)

logger = logging.getLogger(__name__)

# Regex for @mentions (e.g., @Jarvis, @all)
MENTION_PATTERN = re.compile(r"@(\w+)", re.IGNORECASE)

# Base directory for Deep Work project files (visible to user)
_PROJECTS_BASE = Path.home() / "pocketpaw-projects"


def get_project_dir(project_id: str) -> Path:
    """Return the on-disk directory for a project's working files.

    Projects are stored in ~/pocketpaw-projects/{id}/ so they're
    easy to find and browse in the user's home directory.
    """
    return _PROJECTS_BASE / project_id


class MissionControlManager:
    """High-level manager for Mission Control operations.

    Provides convenient methods that handle:
    - Activity logging for all changes
    - Notification creation for @mentions
    - Business logic for task lifecycle
    - Agent heartbeat management
    """

    def __init__(self, store: FileMissionControlStore | None = None):
        """Initialize the manager.

        Args:
            store: Optional store instance. Uses singleton if not provided.
        """
        self._store = store or get_mission_control_store()

    # =========================================================================
    # Agent Operations
    # =========================================================================

    async def create_agent(
        self,
        name: str,
        role: str,
        description: str = "",
        specialties: list[str] | None = None,
        backend: str = "claude_agent_sdk",
    ) -> AgentProfile:
        """Create a new agent and log the activity.

        Args:
            name: Display name (e.g., "Jarvis")
            role: Job title (e.g., "Squad Lead")
            description: Personality/capabilities description
            specialties: List of skills this agent has
            backend: Agent backend to use

        Returns:
            The created AgentProfile
        """
        agent = AgentProfile(
            name=name,
            role=role,
            description=description,
            session_key=f"agent:{name.lower()}:main",
            backend=backend,
            specialties=specialties or [],
        )

        await self._store.save_agent(agent)

        # Log activity
        await self._log_activity(
            ActivityType.AGENT_STATUS_CHANGED,
            agent_id=agent.id,
            message=f"{name} joined the team as {role}",
        )

        logger.info(f"Created agent: {name} ({role})")
        return agent

    async def get_agent(self, agent_id: str) -> AgentProfile | None:
        """Get an agent by ID."""
        return await self._store.get_agent(agent_id)

    async def get_agent_by_name(self, name: str) -> AgentProfile | None:
        """Get an agent by name."""
        return await self._store.get_agent_by_name(name)

    async def list_agents(self, status: str | None = None) -> list[AgentProfile]:
        """List all agents, optionally filtered by status."""
        return await self._store.list_agents(status)

    async def update_agent(self, agent: AgentProfile) -> str:
        """Update an agent profile."""
        return await self._store.save_agent(agent)

    async def record_heartbeat(self, agent_id: str) -> bool:
        """Record an agent heartbeat.

        Updates last_heartbeat timestamp and resets status to IDLE.
        """
        success = await self._store.update_agent_heartbeat(agent_id)
        if success:
            agent = await self._store.get_agent(agent_id)
            if agent:
                await self._log_activity(
                    ActivityType.AGENT_HEARTBEAT,
                    agent_id=agent_id,
                    message=f"{agent.name} checked in",
                )
        return success

    async def set_agent_status(
        self, agent_id: str, status: AgentStatus, current_task_id: str | None = None
    ) -> bool:
        """Update an agent's status.

        Args:
            agent_id: Agent to update
            status: New status
            current_task_id: Task being worked on (for ACTIVE status)

        Returns:
            True if successful
        """
        agent = await self._store.get_agent(agent_id)
        if not agent:
            return False

        old_status = agent.status
        agent.status = status
        agent.current_task_id = current_task_id

        await self._store.save_agent(agent)

        if old_status != status:
            await self._log_activity(
                ActivityType.AGENT_STATUS_CHANGED,
                agent_id=agent_id,
                message=f"{agent.name} is now {status.value}",
            )

        return True

    # =========================================================================
    # Task Operations
    # =========================================================================

    async def create_task(
        self,
        title: str,
        description: str = "",
        creator_id: str | None = None,
        priority: TaskPriority = TaskPriority.MEDIUM,
        tags: list[str] | None = None,
        assignee_ids: list[str] | None = None,
    ) -> Task:
        """Create a new task with activity logging.

        Args:
            title: Short task summary
            description: Full details
            creator_id: Agent/user who created this
            priority: Urgency level
            tags: Categorization tags
            assignee_ids: Agents to assign (optional)

        Returns:
            The created Task
        """
        status = TaskStatus.INBOX
        if assignee_ids:
            status = TaskStatus.ASSIGNED

        task = Task(
            title=title,
            description=description,
            creator_id=creator_id,
            priority=priority,
            status=status,
            tags=tags or [],
            assignee_ids=assignee_ids or [],
        )

        await self._store.save_task(task)

        # Log activity
        await self._log_activity(
            ActivityType.TASK_CREATED,
            agent_id=creator_id,
            task_id=task.id,
            message=f"Created task: {title}",
        )

        # Notify assignees
        if assignee_ids:
            for aid in assignee_ids:
                await self._create_notification(
                    aid,
                    ActivityType.TASK_ASSIGNED,
                    f"You were assigned to: {title}",
                    task_id=task.id,
                )

        logger.info(f"Created task: {title}")
        return task

    async def get_task(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        return await self._store.get_task(task_id)

    async def save_task(self, task: Task) -> str:
        """Save or update a task (low-level).

        Use this instead of accessing _store directly to keep writes
        routed through the manager.

        Args:
            task: Task to save.

        Returns:
            The task ID.
        """
        return await self._store.save_task(task)

    async def list_tasks(
        self,
        status: TaskStatus | None = None,
        assignee_id: str | None = None,
        tags: list[str] | None = None,
    ) -> list[Task]:
        """List tasks with optional filters."""
        return await self._store.list_tasks(status, assignee_id, tags)

    async def assign_task(self, task_id: str, agent_ids: list[str]) -> bool:
        """Assign a task to agents.

        Args:
            task_id: Task to assign
            agent_ids: Agents to assign

        Returns:
            True if successful
        """
        task = await self._store.get_task(task_id)
        if not task:
            return False

        # Add new assignees (don't duplicate)
        new_assignees = [aid for aid in agent_ids if aid not in task.assignee_ids]
        task.assignee_ids.extend(new_assignees)

        if task.status == TaskStatus.INBOX:
            task.status = TaskStatus.ASSIGNED

        await self._store.save_task(task)

        # Notify new assignees
        for aid in new_assignees:
            agent = await self._store.get_agent(aid)
            agent_name = agent.name if agent else "Unknown"

            await self._log_activity(
                ActivityType.TASK_ASSIGNED,
                agent_id=aid,
                task_id=task_id,
                message=f"{agent_name} assigned to: {task.title}",
            )

            await self._create_notification(
                aid,
                ActivityType.TASK_ASSIGNED,
                f"You were assigned to: {task.title}",
                task_id=task_id,
            )

        return True

    async def update_task_status(
        self, task_id: str, status: TaskStatus, agent_id: str | None = None
    ) -> bool:
        """Update a task's status with activity logging.

        Args:
            task_id: Task to update
            status: New status
            agent_id: Agent making the change

        Returns:
            True if successful
        """
        task = await self._store.get_task(task_id)
        if not task:
            return False

        old_status = task.status
        task.status = status

        # Set timestamps
        if status == TaskStatus.IN_PROGRESS and not task.started_at:
            task.started_at = now_iso()
        elif status == TaskStatus.DONE and not task.completed_at:
            task.completed_at = now_iso()

        await self._store.save_task(task)

        # Log activity
        activity_type = (
            ActivityType.TASK_COMPLETED if status == TaskStatus.DONE else ActivityType.TASK_UPDATED
        )
        await self._log_activity(
            activity_type,
            agent_id=agent_id,
            task_id=task_id,
            message=f"Task '{task.title}' moved from {old_status.value} to {status.value}",
        )

        return True

    async def get_tasks_for_agent(self, agent_id: str) -> list[Task]:
        """Get all tasks assigned to an agent."""
        return await self._store.get_tasks_for_agent(agent_id)

    # =========================================================================
    # Message Operations
    # =========================================================================

    async def post_message(
        self,
        task_id: str,
        from_agent_id: str,
        content: str,
        attachment_ids: list[str] | None = None,
    ) -> Message:
        """Post a message to a task thread.

        Automatically extracts @mentions and creates notifications.

        Args:
            task_id: Task to comment on
            from_agent_id: Agent posting the message
            content: Message text (can contain @mentions)
            attachment_ids: Optional document attachments

        Returns:
            The created Message
        """
        # Extract @mentions
        mentions = self._extract_mentions(content)

        message = Message(
            task_id=task_id,
            from_agent_id=from_agent_id,
            content=content,
            attachment_ids=attachment_ids or [],
            mentions=mentions,
        )

        await self._store.save_message(message)

        # Get sender name for activity
        sender = await self._store.get_agent(from_agent_id)
        sender_name = sender.name if sender else "Unknown"

        task = await self._store.get_task(task_id)
        task_title = task.title if task else "Unknown task"

        # Log activity
        await self._log_activity(
            ActivityType.MESSAGE_SENT,
            agent_id=from_agent_id,
            task_id=task_id,
            message=f"{sender_name} commented on '{task_title}'",
        )

        # Create notifications for mentions
        await self._notify_mentions(mentions, message, sender_name, task_title)

        return message

    async def get_messages_for_task(self, task_id: str) -> list[Message]:
        """Get all messages for a task."""
        return await self._store.get_messages_for_task(task_id)

    def _extract_mentions(self, content: str) -> list[str]:
        """Extract @mentions from content.

        Returns list of mentioned names (lowercase).
        """
        matches = MENTION_PATTERN.findall(content)
        return [m.lower() for m in matches]

    async def _notify_mentions(
        self,
        mentions: list[str],
        message: Message,
        sender_name: str,
        task_title: str,
    ) -> None:
        """Create notifications for @mentions."""
        for mention in mentions:
            if mention == "all":
                # Notify all agents
                agents = await self._store.list_agents()
                for agent in agents:
                    if agent.id != message.from_agent_id:
                        await self._create_notification(
                            agent.id,
                            ActivityType.MENTION,
                            f"{sender_name} mentioned @all in '{task_title}'",
                            task_id=message.task_id,
                            message_id=message.id,
                        )
            else:
                # Notify specific agent
                agent = await self._store.get_agent_by_name(mention)
                if agent and agent.id != message.from_agent_id:
                    await self._create_notification(
                        agent.id,
                        ActivityType.MENTION,
                        f"{sender_name} mentioned you in '{task_title}'",
                        task_id=message.task_id,
                        message_id=message.id,
                    )

    # =========================================================================
    # Document Operations
    # =========================================================================

    async def create_document(
        self,
        title: str,
        content: str,
        doc_type: DocumentType = DocumentType.DRAFT,
        author_id: str | None = None,
        task_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Document:
        """Create a new document.

        Args:
            title: Document title
            content: Document content (usually markdown)
            doc_type: Type of document
            author_id: Agent who created it
            task_id: Associated task
            tags: Categorization tags

        Returns:
            The created Document
        """
        document = Document(
            title=title,
            content=content,
            type=doc_type,
            author_id=author_id,
            task_id=task_id,
            tags=tags or [],
        )

        await self._store.save_document(document)

        # Get author name
        author = await self._store.get_agent(author_id) if author_id else None
        author_name = author.name if author else "Unknown"

        # Log activity
        await self._log_activity(
            ActivityType.DOCUMENT_CREATED,
            agent_id=author_id,
            task_id=task_id,
            document_id=document.id,
            message=f"{author_name} created document: {title}",
        )

        return document

    async def save_document(self, document: Document) -> str:
        """Save or update a document (low-level)."""
        return await self._store.save_document(document)

    async def save_activity(self, activity: Activity) -> str:
        """Save an activity (low-level)."""
        return await self._store.save_activity(activity)

    async def get_document(self, document_id: str) -> Document | None:
        """Get a document by ID."""
        return await self._store.get_document(document_id)

    async def list_documents(
        self,
        doc_type: str | None = None,
        task_id: str | None = None,
        tags: list[str] | None = None,
    ) -> list[Document]:
        """List documents with optional filters."""
        return await self._store.list_documents(doc_type, task_id, tags)

    async def get_task_documents(self, task_id: str) -> list[Document]:
        """Get all documents linked to a task."""
        return await self._store.list_documents(task_id=task_id)

    async def update_document(
        self, document_id: str, content: str, editor_id: str | None = None
    ) -> Document | None:
        """Update a document's content.

        Automatically increments version.

        Args:
            document_id: Document to update
            content: New content
            editor_id: Agent making the edit

        Returns:
            Updated Document or None if not found
        """
        document = await self._store.get_document(document_id)
        if not document:
            return None

        document.content = content
        await self._store.save_document(document)

        # Get editor name
        editor = await self._store.get_agent(editor_id) if editor_id else None
        editor_name = editor.name if editor else "Unknown"

        # Log activity
        await self._log_activity(
            ActivityType.DOCUMENT_UPDATED,
            agent_id=editor_id,
            document_id=document_id,
            message=f"{editor_name} updated document: {document.title} (v{document.version})",
        )

        return document

    # =========================================================================
    # Project Operations (Deep Work)
    # =========================================================================

    async def create_project(
        self,
        title: str,
        description: str = "",
        creator_id: str = "human",
        tags: list[str] | None = None,
    ) -> Project:
        """Create a new project and log the activity.

        Args:
            title: Short project name
            description: Full project description and goals
            creator_id: Agent or user who created this (default "human")
            tags: Categorization tags

        Returns:
            The created Project
        """
        from pocketclaw.deep_work.models import Project

        project = Project(
            title=title,
            description=description,
            creator_id=creator_id,
            tags=tags or [],
        )

        await self._store.save_project(project)

        # Create project directory on disk
        project_dir = get_project_dir(project.id)
        project_dir.mkdir(parents=True, exist_ok=True)

        # Log activity (reuse TASK_CREATED for project creation)
        await self._log_activity(
            ActivityType.TASK_CREATED,
            agent_id=creator_id if creator_id != "human" else None,
            message=f"Created project: {title}",
        )

        logger.info(f"Created project: {title} (dir: {project_dir})")
        return project

    async def get_project(self, project_id: str) -> Project | None:
        """Get a project by ID."""
        return await self._store.get_project(project_id)

    async def list_projects(self, status: str | None = None) -> list[Project]:
        """List all projects, optionally filtered by status."""
        return await self._store.list_projects(status)

    async def get_project_tasks(self, project_id: str) -> list[Task]:
        """Get all tasks belonging to a project.

        Args:
            project_id: Project to get tasks for

        Returns:
            List of tasks with matching project_id
        """
        all_tasks = await self._store.list_tasks(limit=0)
        return [t for t in all_tasks if t.project_id == project_id]

    async def get_project_progress(self, project_id: str) -> dict[str, Any]:
        """Get progress summary for a project.

        Args:
            project_id: Project to get progress for

        Returns:
            Dict with total, completed, skipped, in_progress, blocked, human_pending, percent
        """
        tasks = await self.get_project_tasks(project_id)
        total = len(tasks)
        completed = len([t for t in tasks if t.status == TaskStatus.DONE])
        skipped = len([t for t in tasks if t.status == TaskStatus.SKIPPED])
        in_progress = len([t for t in tasks if t.status == TaskStatus.IN_PROGRESS])
        blocked = len([t for t in tasks if t.status == TaskStatus.BLOCKED])
        human_pending = len(
            [
                t
                for t in tasks
                if t.task_type == "human" and t.status not in (TaskStatus.DONE, TaskStatus.SKIPPED)
            ]
        )
        percent = ((completed + skipped) / total * 100) if total > 0 else 0.0

        return {
            "total": total,
            "completed": completed,
            "skipped": skipped,
            "in_progress": in_progress,
            "blocked": blocked,
            "human_pending": human_pending,
            "percent": round(percent, 1),
        }

    async def update_project(self, project: Project) -> str:
        """Update a project.

        Args:
            project: Project instance to save

        Returns:
            The project ID
        """
        return await self._store.save_project(project)

    async def delete_project(self, project_id: str) -> bool:
        """Delete a project and all its tasks.

        Also removes the project directory from disk.

        Args:
            project_id: Project to delete

        Returns:
            True if successfully deleted
        """
        # Delete project's tasks first
        tasks = await self.get_project_tasks(project_id)
        for task in tasks:
            await self._store.delete_task(task.id)

        # Remove project directory from disk
        project_dir = get_project_dir(project_id)
        if project_dir.exists():
            shutil.rmtree(project_dir)
            logger.info(f"Removed project directory: {project_dir}")

        return await self._store.delete_project(project_id)

    async def ensure_project_directories(self) -> int:
        """Create directories for existing projects that lack one.

        Called at startup to migrate projects that existed before
        directory management was added.

        Returns:
            Number of directories created
        """
        projects = await self._store.list_projects()
        created = 0
        for project in projects:
            project_dir = get_project_dir(project.id)
            if not project_dir.exists():
                project_dir.mkdir(parents=True, exist_ok=True)
                created += 1
                logger.info(f"Created missing project directory: {project_dir}")
        if created:
            logger.info(f"Created {created} missing project directories")
        return created

    # =========================================================================
    # Activity & Notification Operations
    # =========================================================================

    async def get_activity_feed(self, limit: int = 50) -> list[Activity]:
        """Get the activity feed (most recent first)."""
        return await self._store.get_activity_feed(limit)

    async def get_notifications_for_agent(
        self, agent_id: str, unread_only: bool = False
    ) -> list[Notification]:
        """Get notifications for an agent."""
        return await self._store.get_notifications_for_agent(agent_id, unread_only)

    async def get_undelivered_notifications(
        self, agent_id: str | None = None
    ) -> list[Notification]:
        """Get undelivered notifications."""
        return await self._store.get_undelivered_notifications(agent_id)

    async def mark_notification_delivered(self, notification_id: str) -> bool:
        """Mark a notification as delivered."""
        return await self._store.mark_notification_delivered(notification_id)

    async def mark_notification_read(self, notification_id: str) -> bool:
        """Mark a notification as read."""
        return await self._store.mark_notification_read(notification_id)

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _log_activity(
        self,
        activity_type: ActivityType,
        agent_id: str | None = None,
        task_id: str | None = None,
        document_id: str | None = None,
        message: str = "",
    ) -> Activity:
        """Create and save an activity entry."""
        activity = Activity(
            type=activity_type,
            agent_id=agent_id,
            task_id=task_id,
            document_id=document_id,
            message=message,
        )
        await self._store.save_activity(activity)
        return activity

    async def _create_notification(
        self,
        agent_id: str,
        notification_type: ActivityType,
        content: str,
        task_id: str | None = None,
        message_id: str | None = None,
    ) -> Notification:
        """Create a notification for an agent."""
        notification = Notification(
            agent_id=agent_id,
            type=notification_type,
            content=content,
            source_task_id=task_id,
            source_message_id=message_id,
        )
        await self._store.save_notification(notification)
        return notification

    # =========================================================================
    # Standup & Reports
    # =========================================================================

    async def generate_standup(self) -> str:
        """Generate a daily standup summary.

        Returns markdown-formatted standup with:
        - Completed tasks
        - In-progress tasks
        - Blocked tasks
        - Agent status
        """
        from datetime import UTC, datetime

        today = datetime.now(UTC).strftime("%b %d, %Y")

        lines = [f"# Daily Standup - {today}\n"]

        # Completed tasks
        done_tasks = await self._store.list_tasks(status=TaskStatus.DONE, limit=10)
        if done_tasks:
            lines.append("## Completed")
            for task in done_tasks:
                assignees = []
                for aid in task.assignee_ids:
                    agent = await self._store.get_agent(aid)
                    if agent:
                        assignees.append(agent.name)
                assignee_str = ", ".join(assignees) if assignees else "Unassigned"
                lines.append(f"- {task.title} ({assignee_str})")
            lines.append("")

        # In-progress tasks
        in_progress = await self._store.list_tasks(status=TaskStatus.IN_PROGRESS, limit=10)
        if in_progress:
            lines.append("## In Progress")
            for task in in_progress:
                assignees = []
                for aid in task.assignee_ids:
                    agent = await self._store.get_agent(aid)
                    if agent:
                        assignees.append(agent.name)
                assignee_str = ", ".join(assignees) if assignees else "Unassigned"
                lines.append(f"- {task.title} ({assignee_str})")
            lines.append("")

        # Blocked tasks
        blocked = await self._store.get_blocked_tasks()
        if blocked:
            lines.append("## Blocked")
            for task in blocked:
                lines.append(f"- {task.title}")
            lines.append("")

        # Agent status
        agents = await self._store.list_agents()
        if agents:
            lines.append("## Team Status")
            for agent in agents:
                status_emoji = {
                    AgentStatus.IDLE: "💤",
                    AgentStatus.ACTIVE: "🟢",
                    AgentStatus.BLOCKED: "🔴",
                    AgentStatus.OFFLINE: "⚫",
                }.get(agent.status, "❓")
                lines.append(f"- {status_emoji} {agent.name} ({agent.role}): {agent.status.value}")
            lines.append("")

        return "\n".join(lines)

    async def get_stats(self) -> dict[str, Any]:
        """Get Mission Control statistics."""
        return await self._store.get_stats()


# =========================================================================
# Factory Function
# =========================================================================

_manager_instance: MissionControlManager | None = None


def get_mission_control_manager() -> MissionControlManager:
    """Get or create the Mission Control manager singleton."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = MissionControlManager()
    return _manager_instance


def reset_mission_control_manager() -> None:
    """Reset the manager singleton (for testing)."""
    global _manager_instance
    _manager_instance = None
