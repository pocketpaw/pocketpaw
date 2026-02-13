# Deep Work models — Project orchestration layer.
# Created: 2026-02-12
#
# Defines data structures for:
# - Project: top-level orchestration unit grouping tasks and agents
# - TaskSpec: lightweight task blueprint from the planner (not yet a MC Task)
# - AgentSpec: recommended agent blueprint from the planner
# - PlannerResult: full output from the planning phase

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pocketclaw.mission_control.models import generate_id, now_iso

# ============================================================================
# Enums
# ============================================================================


class ProjectStatus(str, Enum):
    """Project lifecycle status."""

    DRAFT = "draft"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


# ============================================================================
# Data Models
# ============================================================================


@dataclass
class Project:
    """Represents a Deep Work project.

    A project groups related tasks, agents, and deliverables under
    a single orchestration unit with lifecycle management.

    Attributes:
        id: Unique identifier
        title: Short project name
        description: Full project description and goals
        status: Current lifecycle status
        planner_agent_id: Agent responsible for planning this project
        team_agent_ids: Agents assigned to work on this project
        task_ids: MC Task IDs belonging to this project
        prd_document_id: MC Document ID for the project PRD
        creator_id: Who created this project (agent ID or "human")
        tags: Categorization tags
        started_at: When execution began
        completed_at: When project finished
        created_at: When project was created
        updated_at: Last modification time
        metadata: Extensible key-value data
    """

    id: str = field(default_factory=generate_id)
    title: str = ""
    description: str = ""
    status: ProjectStatus = ProjectStatus.DRAFT
    planner_agent_id: str | None = None
    team_agent_ids: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    prd_document_id: str | None = None
    creator_id: str = "human"
    tags: list[str] = field(default_factory=list)
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "planner_agent_id": self.planner_agent_id,
            "team_agent_ids": self.team_agent_ids,
            "task_ids": self.task_ids,
            "prd_document_id": self.prd_document_id,
            "creator_id": self.creator_id,
            "tags": self.tags,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        """Create from dictionary."""
        return cls(
            id=data.get("id", generate_id()),
            title=data.get("title", ""),
            description=data.get("description", ""),
            status=ProjectStatus(data.get("status", "draft")),
            planner_agent_id=data.get("planner_agent_id"),
            team_agent_ids=data.get("team_agent_ids", []),
            task_ids=data.get("task_ids", []),
            prd_document_id=data.get("prd_document_id"),
            creator_id=data.get("creator_id", "human"),
            tags=data.get("tags", []),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            created_at=data.get("created_at", now_iso()),
            updated_at=data.get("updated_at", now_iso()),
            metadata=data.get("metadata", {}),
        )


@dataclass
class TaskSpec:
    """Lightweight task blueprint from the planner.

    TaskSpec is a planning-phase artifact — it describes work to be done
    but has not yet been materialized as a Mission Control Task.

    Attributes:
        key: Short unique key within the project (e.g., "research-competitors")
        title: Human-readable task title
        description: Full task description and acceptance criteria
        task_type: "agent" | "human" | "review"
        priority: "low" | "medium" | "high" | "urgent"
        tags: Categorization tags
        estimated_minutes: Estimated time to complete
        required_specialties: Agent specialties needed for this task
        blocked_by_keys: Keys of other TaskSpecs this depends on
    """

    key: str = ""
    title: str = ""
    description: str = ""
    task_type: str = "agent"
    priority: str = "medium"
    tags: list[str] = field(default_factory=list)
    estimated_minutes: int = 30
    required_specialties: list[str] = field(default_factory=list)
    blocked_by_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "task_type": self.task_type,
            "priority": self.priority,
            "tags": self.tags,
            "estimated_minutes": self.estimated_minutes,
            "required_specialties": self.required_specialties,
            "blocked_by_keys": self.blocked_by_keys,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        """Create from dictionary."""
        return cls(
            key=data.get("key", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            task_type=data.get("task_type", "agent"),
            priority=data.get("priority", "medium"),
            tags=data.get("tags", []),
            estimated_minutes=data.get("estimated_minutes", 30),
            required_specialties=data.get("required_specialties", []),
            blocked_by_keys=data.get("blocked_by_keys", []),
        )


@dataclass
class AgentSpec:
    """Recommended agent blueprint from the planner.

    AgentSpec describes a recommended team member — it has not yet been
    materialized as a Mission Control AgentProfile.

    Attributes:
        name: Suggested agent name
        role: Job title/function
        description: What this agent does
        specialties: Skills/domains this agent should have
        backend: Which agent backend to use
    """

    name: str = ""
    role: str = ""
    description: str = ""
    specialties: list[str] = field(default_factory=list)
    backend: str = "claude_agent_sdk"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "specialties": self.specialties,
            "backend": self.backend,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSpec":
        """Create from dictionary."""
        return cls(
            name=data.get("name", ""),
            role=data.get("role", ""),
            description=data.get("description", ""),
            specialties=data.get("specialties", []),
            backend=data.get("backend", "claude_agent_sdk"),
        )


@dataclass
class PlannerResult:
    """Full output from the planning phase.

    Contains everything needed to materialize a project: PRD content,
    task breakdown, team recommendations, and dependency graph.

    Attributes:
        project_id: ID of the project this plan belongs to
        prd_content: Full PRD document content (markdown)
        tasks: Ordered list of task blueprints
        team_recommendation: Suggested agents for the project
        human_tasks: Tasks that require human involvement
        dependency_graph: key -> [keys it depends on]
        estimated_total_minutes: Sum of all task estimates
        research_notes: Any research notes from the planner
    """

    project_id: str = ""
    prd_content: str = ""
    tasks: list[TaskSpec] = field(default_factory=list)
    team_recommendation: list[AgentSpec] = field(default_factory=list)
    human_tasks: list[TaskSpec] = field(default_factory=list)
    dependency_graph: dict[str, list[str]] = field(default_factory=dict)
    estimated_total_minutes: int = 0
    research_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "project_id": self.project_id,
            "prd_content": self.prd_content,
            "tasks": [t.to_dict() for t in self.tasks],
            "team_recommendation": [a.to_dict() for a in self.team_recommendation],
            "human_tasks": [t.to_dict() for t in self.human_tasks],
            "dependency_graph": self.dependency_graph,
            "estimated_total_minutes": self.estimated_total_minutes,
            "research_notes": self.research_notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlannerResult":
        """Create from dictionary."""
        return cls(
            project_id=data.get("project_id", ""),
            prd_content=data.get("prd_content", ""),
            tasks=[TaskSpec.from_dict(t) for t in data.get("tasks", [])],
            team_recommendation=[
                AgentSpec.from_dict(a) for a in data.get("team_recommendation", [])
            ],
            human_tasks=[TaskSpec.from_dict(t) for t in data.get("human_tasks", [])],
            dependency_graph=data.get("dependency_graph", {}),
            estimated_total_minutes=data.get("estimated_total_minutes", 0),
            research_notes=data.get("research_notes", ""),
        )
