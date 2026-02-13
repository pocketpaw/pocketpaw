"""Mission Control data models.

Created: 2026-02-05
Updated: 2026-02-12 — Added SKIPPED status to TaskStatus enum for Deep Work
  skip-task feature. Extended Task model with Deep Work fields:
  - project_id: optional project grouping
  - task_type: "agent" | "human" | "review"
  - blocks: list of task IDs this task blocks
  - active_description: present-continuous description for spinner UI
  - estimated_minutes: optional time estimate

Part of Mission Control feature for multi-agent orchestration.

These models define the core data structures for:
- Agent profiles (identity, status, capabilities)
- Tasks (work items with lifecycle)
- Messages (comments/discussions on tasks)
- Activities (audit trail/feed)
- Documents (shared deliverables)
- Notifications (@mentions, alerts)

Design notes:
- Uses dataclasses for simplicity (like MemoryEntry)
- All IDs are UUIDs for uniqueness
- Timestamps are ISO 8601 strings for JSON serialization
- Status enums for type safety
- Metadata dicts for extensibility
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

# ============================================================================
# Enums
# ============================================================================


class AgentStatus(str, Enum):
    """Agent operational status."""

    IDLE = "idle"  # Not actively working
    ACTIVE = "active"  # Currently processing
    BLOCKED = "blocked"  # Waiting on something
    OFFLINE = "offline"  # Not responding to heartbeats


class AgentLevel(str, Enum):
    """Agent autonomy level."""

    INTERN = "intern"  # Needs approval for most actions
    SPECIALIST = "specialist"  # Works independently in domain
    LEAD = "lead"  # Full autonomy, can delegate


class TaskStatus(str, Enum):
    """Task lifecycle status."""

    INBOX = "inbox"  # New, unassigned
    ASSIGNED = "assigned"  # Has owner(s), not started
    IN_PROGRESS = "in_progress"  # Being worked on
    REVIEW = "review"  # Done, needs approval
    DONE = "done"  # Completed
    BLOCKED = "blocked"  # Stuck, needs resolution
    SKIPPED = "skipped"  # Manually skipped by user


class TaskPriority(str, Enum):
    """Task priority level."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class ActivityType(str, Enum):
    """Types of activities for the feed."""

    TASK_CREATED = "task_created"
    TASK_UPDATED = "task_updated"
    TASK_ASSIGNED = "task_assigned"
    TASK_COMPLETED = "task_completed"
    MESSAGE_SENT = "message_sent"
    DOCUMENT_CREATED = "document_created"
    DOCUMENT_UPDATED = "document_updated"
    AGENT_HEARTBEAT = "agent_heartbeat"
    AGENT_STATUS_CHANGED = "agent_status_changed"
    MENTION = "mention"


class DocumentType(str, Enum):
    """Types of shared documents."""

    DELIVERABLE = "deliverable"  # Final output
    RESEARCH = "research"  # Research notes
    PROTOCOL = "protocol"  # Process docs
    TEMPLATE = "template"  # Reusable template
    DRAFT = "draft"  # Work in progress


# ============================================================================
# Helper Functions
# ============================================================================


def generate_id() -> str:
    """Generate a unique ID."""
    return str(uuid.uuid4())


def now_iso() -> str:
    """Get current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


# ============================================================================
# Data Models
# ============================================================================


@dataclass
class AgentProfile:
    """
    Represents an AI agent in the Mission Control system.

    Each agent has a unique identity, role, and capabilities.
    Maps to a PocketPaw session/backend for execution.

    Attributes:
        id: Unique identifier
        name: Display name (e.g., "Jarvis", "Shuri")
        role: Job title/function (e.g., "Squad Lead", "Product Analyst")
        description: What this agent does, their personality
        session_key: Maps to PocketPaw session for execution
        backend: Which agent backend to use (claude_agent_sdk, open_interpreter, etc.)
        status: Current operational status
        level: Autonomy level
        current_task_id: ID of task being worked on (if any)
        specialties: List of skills/domains this agent excels at
        last_heartbeat: Last time this agent checked in
        created_at: When this agent was created
        updated_at: Last modification time
        metadata: Extensible key-value data
    """

    id: str = field(default_factory=generate_id)
    name: str = ""
    role: str = ""
    description: str = ""
    session_key: str = ""
    backend: str = "claude_agent_sdk"
    status: AgentStatus = AgentStatus.IDLE
    level: AgentLevel = AgentLevel.SPECIALIST
    current_task_id: str | None = None
    specialties: list[str] = field(default_factory=list)
    last_heartbeat: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "session_key": self.session_key,
            "backend": self.backend,
            "status": self.status.value,
            "level": self.level.value,
            "current_task_id": self.current_task_id,
            "specialties": self.specialties,
            "last_heartbeat": self.last_heartbeat,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentProfile":
        """Create from dictionary."""
        return cls(
            id=data.get("id", generate_id()),
            name=data.get("name", ""),
            role=data.get("role", ""),
            description=data.get("description", ""),
            session_key=data.get("session_key", ""),
            backend=data.get("backend", "claude_agent_sdk"),
            status=AgentStatus(data.get("status", "idle")),
            level=AgentLevel(data.get("level", "specialist")),
            current_task_id=data.get("current_task_id"),
            specialties=data.get("specialties", []),
            last_heartbeat=data.get("last_heartbeat"),
            created_at=data.get("created_at", now_iso()),
            updated_at=data.get("updated_at", now_iso()),
            metadata=data.get("metadata", {}),
        )


@dataclass
class Task:
    """
    Represents a work item in Mission Control.

    Tasks flow through a lifecycle: inbox -> assigned -> in_progress -> review -> done
    Can be blocked if waiting on dependencies.

    Attributes:
        id: Unique identifier
        title: Short summary of the task
        description: Full details, requirements, context
        status: Current lifecycle status
        priority: Urgency level
        assignee_ids: List of agent IDs assigned to this task
        creator_id: Agent or user who created the task
        parent_task_id: For subtasks, the parent task ID
        blocked_by: List of task IDs this depends on
        tags: Categorization tags
        due_date: Optional deadline (ISO 8601)
        started_at: When work began
        completed_at: When task was done
        created_at: When task was created
        updated_at: Last modification time
        metadata: Extensible key-value data
        project_id: Optional project grouping ID (Deep Work)
        task_type: Task type - "agent", "human", or "review" (Deep Work)
        blocks: List of task IDs this task blocks (Deep Work)
        active_description: Present-continuous description for spinner UI (Deep Work)
        estimated_minutes: Optional time estimate in minutes (Deep Work)
    """

    id: str = field(default_factory=generate_id)
    title: str = ""
    description: str = ""
    status: TaskStatus = TaskStatus.INBOX
    priority: TaskPriority = TaskPriority.MEDIUM
    assignee_ids: list[str] = field(default_factory=list)
    creator_id: str | None = None
    parent_task_id: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    due_date: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)
    project_id: str | None = None
    task_type: str = "agent"
    blocks: list[str] = field(default_factory=list)
    active_description: str = ""
    estimated_minutes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority.value,
            "assignee_ids": self.assignee_ids,
            "creator_id": self.creator_id,
            "parent_task_id": self.parent_task_id,
            "blocked_by": self.blocked_by,
            "tags": self.tags,
            "due_date": self.due_date,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
            "project_id": self.project_id,
            "task_type": self.task_type,
            "blocks": self.blocks,
            "active_description": self.active_description,
            "estimated_minutes": self.estimated_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        """Create from dictionary."""
        return cls(
            id=data.get("id", generate_id()),
            title=data.get("title", ""),
            description=data.get("description", ""),
            status=TaskStatus(data.get("status", "inbox")),
            priority=TaskPriority(data.get("priority", "medium")),
            assignee_ids=data.get("assignee_ids", []),
            creator_id=data.get("creator_id"),
            parent_task_id=data.get("parent_task_id"),
            blocked_by=data.get("blocked_by", []),
            tags=data.get("tags", []),
            due_date=data.get("due_date"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            created_at=data.get("created_at", now_iso()),
            updated_at=data.get("updated_at", now_iso()),
            metadata=data.get("metadata", {}),
            project_id=data.get("project_id"),
            task_type=data.get("task_type", "agent"),
            blocks=data.get("blocks", []),
            active_description=data.get("active_description", ""),
            estimated_minutes=data.get("estimated_minutes"),
        )


@dataclass
class Message:
    """
    Represents a comment/message on a task.

    Messages form discussion threads on tasks. They can have attachments
    and can mention other agents using @name syntax.

    Attributes:
        id: Unique identifier
        task_id: Task this message belongs to
        from_agent_id: Agent who sent the message
        content: The message text (can contain @mentions)
        attachment_ids: List of document IDs attached
        mentions: Extracted @mentions from content
        created_at: When message was sent
        metadata: Extensible key-value data
    """

    id: str = field(default_factory=generate_id)
    task_id: str = ""
    from_agent_id: str = ""
    content: str = ""
    attachment_ids: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "from_agent_id": self.from_agent_id,
            "content": self.content,
            "attachment_ids": self.attachment_ids,
            "mentions": self.mentions,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        """Create from dictionary."""
        return cls(
            id=data.get("id", generate_id()),
            task_id=data.get("task_id", ""),
            from_agent_id=data.get("from_agent_id", ""),
            content=data.get("content", ""),
            attachment_ids=data.get("attachment_ids", []),
            mentions=data.get("mentions", []),
            created_at=data.get("created_at", now_iso()),
            metadata=data.get("metadata", {}),
        )


@dataclass
class Activity:
    """
    Represents an entry in the activity feed.

    Activities provide an audit trail and real-time visibility
    into what's happening across the system.

    Attributes:
        id: Unique identifier
        type: Type of activity
        agent_id: Agent who triggered this activity
        message: Human-readable description
        task_id: Related task (if applicable)
        document_id: Related document (if applicable)
        created_at: When activity occurred
        metadata: Additional context data
    """

    id: str = field(default_factory=generate_id)
    type: ActivityType = ActivityType.TASK_CREATED
    agent_id: str | None = None
    message: str = ""
    task_id: str | None = None
    document_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "type": self.type.value,
            "agent_id": self.agent_id,
            "message": self.message,
            "task_id": self.task_id,
            "document_id": self.document_id,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Activity":
        """Create from dictionary."""
        return cls(
            id=data.get("id", generate_id()),
            type=ActivityType(data.get("type", "task_created")),
            agent_id=data.get("agent_id"),
            message=data.get("message", ""),
            task_id=data.get("task_id"),
            document_id=data.get("document_id"),
            created_at=data.get("created_at", now_iso()),
            metadata=data.get("metadata", {}),
        )


@dataclass
class Document:
    """
    Represents a shared document/deliverable.

    Documents are the outputs of agent work - research, drafts,
    final deliverables, protocols, etc.

    Attributes:
        id: Unique identifier
        title: Document title
        content: Full content (usually markdown)
        type: Document category
        task_id: Associated task (if any)
        author_id: Agent who created this
        tags: Categorization tags
        version: Version number for tracking changes
        created_at: When created
        updated_at: Last modification time
        metadata: Extensible key-value data
    """

    id: str = field(default_factory=generate_id)
    title: str = ""
    content: str = ""
    type: DocumentType = DocumentType.DRAFT
    task_id: str | None = None
    author_id: str | None = None
    tags: list[str] = field(default_factory=list)
    version: int = 1
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "type": self.type.value,
            "task_id": self.task_id,
            "author_id": self.author_id,
            "tags": self.tags,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Document":
        """Create from dictionary."""
        return cls(
            id=data.get("id", generate_id()),
            title=data.get("title", ""),
            content=data.get("content", ""),
            type=DocumentType(data.get("type", "draft")),
            task_id=data.get("task_id"),
            author_id=data.get("author_id"),
            tags=data.get("tags", []),
            version=data.get("version", 1),
            created_at=data.get("created_at", now_iso()),
            updated_at=data.get("updated_at", now_iso()),
            metadata=data.get("metadata", {}),
        )


@dataclass
class Notification:
    """
    Represents a notification for an agent.

    Notifications are created when agents are @mentioned or when
    important events occur that they should be aware of.

    Attributes:
        id: Unique identifier
        agent_id: Agent to notify
        type: What triggered the notification
        content: Human-readable notification text
        source_message_id: Message that triggered this (if @mention)
        source_task_id: Related task
        delivered: Whether notification was sent to agent
        read: Whether agent has acknowledged it
        created_at: When notification was created
        delivered_at: When it was delivered
        metadata: Extensible key-value data
    """

    id: str = field(default_factory=generate_id)
    agent_id: str = ""
    type: ActivityType = ActivityType.MENTION
    content: str = ""
    source_message_id: str | None = None
    source_task_id: str | None = None
    delivered: bool = False
    read: bool = False
    created_at: str = field(default_factory=now_iso)
    delivered_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "type": self.type.value,
            "content": self.content,
            "source_message_id": self.source_message_id,
            "source_task_id": self.source_task_id,
            "delivered": self.delivered,
            "read": self.read,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Notification":
        """Create from dictionary."""
        return cls(
            id=data.get("id", generate_id()),
            agent_id=data.get("agent_id", ""),
            type=ActivityType(data.get("type", "mention")),
            content=data.get("content", ""),
            source_message_id=data.get("source_message_id"),
            source_task_id=data.get("source_task_id"),
            delivered=data.get("delivered", False),
            read=data.get("read", False),
            created_at=data.get("created_at", now_iso()),
            delivered_at=data.get("delivered_at"),
            metadata=data.get("metadata", {}),
        )
