"""Mission Control API endpoints.

Created: 2026-02-05
Updated: 2026-02-12 — POST /tasks now accepts optional project_id to associate
  a new task with a Deep Work project. Enriched project list/get responses with
  folder_path and file_count for sidebar project browser.
  Previous: Added Deep Work project endpoints:
  - POST /projects — create project
  - GET /projects — list projects (optional status filter)
  - GET /projects/{id} — get project + tasks + progress
  - PATCH /projects/{id} — update project fields
  - DELETE /projects/{id} — delete project
  - POST /projects/{id}/approve — set status to approved
  - POST /projects/{id}/pause — set status to paused
  - POST /projects/{id}/resume — set status to executing

FastAPI router for Mission Control operations.

Provides REST endpoints for:
- Agents: CRUD operations, heartbeat
- Tasks: CRUD, status updates, assignment, execution (run/stop)
- Messages: Post comments with @mentions
- Documents: CRUD for deliverables, task attachments
- Activity: Feed and stats
- Notifications: List and mark read/delivered
- Execution: Run tasks with agents, stop running tasks
- Projects: CRUD and lifecycle management (Deep Work)

Task Execution Endpoints:
- POST /tasks/{id}/run - Start task execution (streams via WebSocket)
- POST /tasks/{id}/stop - Stop running execution
- GET /tasks/running - List currently running tasks
- GET /tasks/{id}/documents - Get documents linked to a task
- POST /tasks/{id}/attachments - Attach a document to a task

Mount this router to your FastAPI app:
    from pocketclaw.mission_control.api import router as mission_control_router
    app.include_router(mission_control_router, prefix="/api/mission-control")
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from pocketclaw.mission_control.manager import get_mission_control_manager
from pocketclaw.mission_control.models import (
    AgentLevel,
    AgentStatus,
    DocumentType,
    TaskPriority,
    TaskStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Mission Control"])


# ============================================================================
# Request/Response Models
# ============================================================================


class CreateAgentRequest(BaseModel):
    """Request to create a new agent."""

    name: str = Field(..., min_length=1, max_length=50)
    role: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="")
    specialties: list[str] = Field(default_factory=list)
    backend: str = Field(default="claude_agent_sdk")
    level: str = Field(default="specialist")


class UpdateAgentRequest(BaseModel):
    """Request to update an agent."""

    name: str | None = None
    role: str | None = None
    description: str | None = None
    specialties: list[str] | None = None
    status: str | None = None
    level: str | None = None


class CreateTaskRequest(BaseModel):
    """Request to create a new task."""

    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="")
    priority: str = Field(default="medium")
    tags: list[str] = Field(default_factory=list)
    assignee_ids: list[str] = Field(default_factory=list)
    creator_id: str | None = None
    project_id: str | None = Field(
        default=None, description="Associate task with a Deep Work project"
    )


class UpdateTaskRequest(BaseModel):
    """Request to update a task."""

    title: str | None = None
    description: str | None = None
    priority: str | None = None
    status: str | None = None
    tags: list[str] | None = None


class AssignTaskRequest(BaseModel):
    """Request to assign agents to a task."""

    agent_ids: list[str]


class UpdateTaskStatusRequest(BaseModel):
    """Request to update a task's status."""

    status: str
    agent_id: str | None = None


class PostMessageRequest(BaseModel):
    """Request to post a message on a task."""

    from_agent_id: str
    content: str = Field(..., min_length=1)
    attachment_ids: list[str] = Field(default_factory=list)


class CreateDocumentRequest(BaseModel):
    """Request to create a document."""

    title: str = Field(..., min_length=1, max_length=200)
    content: str
    type: str = Field(default="draft")
    author_id: str | None = None
    task_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class UpdateDocumentRequest(BaseModel):
    """Request to update a document."""

    content: str
    editor_id: str | None = None


class SuccessResponse(BaseModel):
    """Generic success response."""

    success: bool = True
    message: str = ""


# ============================================================================
# Agent Endpoints
# ============================================================================


@router.get("/agents")
async def list_agents(status: str | None = None, limit: int = 100) -> dict[str, Any]:
    """List all agents, optionally filtered by status."""
    manager = get_mission_control_manager()
    agents = await manager.list_agents(status)
    return {
        "agents": [a.to_dict() for a in agents[:limit]],
        "count": len(agents),
    }


@router.post("/agents")
async def create_agent(request: CreateAgentRequest) -> dict[str, Any]:
    """Create a new agent."""
    manager = get_mission_control_manager()

    agent = await manager.create_agent(
        name=request.name,
        role=request.role,
        description=request.description,
        specialties=request.specialties,
        backend=request.backend,
    )

    # Set level if not default
    if request.level != "specialist":
        agent.level = AgentLevel(request.level)
        await manager.update_agent(agent)

    return {"agent": agent.to_dict()}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict[str, Any]:
    """Get an agent by ID."""
    manager = get_mission_control_manager()
    agent = await manager.get_agent(agent_id)

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return {"agent": agent.to_dict()}


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, request: UpdateAgentRequest) -> dict[str, Any]:
    """Update an agent's details."""
    manager = get_mission_control_manager()
    agent = await manager.get_agent(agent_id)

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if request.name is not None:
        agent.name = request.name
    if request.role is not None:
        agent.role = request.role
    if request.description is not None:
        agent.description = request.description
    if request.specialties is not None:
        agent.specialties = request.specialties
    if request.status is not None:
        agent.status = AgentStatus(request.status)
    if request.level is not None:
        agent.level = AgentLevel(request.level)

    await manager.update_agent(agent)
    return {"agent": agent.to_dict()}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str) -> SuccessResponse:
    """Delete an agent."""
    manager = get_mission_control_manager()
    store = manager._store

    deleted = await store.delete_agent(agent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent not found")

    return SuccessResponse(message=f"Agent {agent_id} deleted")


@router.post("/agents/{agent_id}/heartbeat")
async def record_heartbeat(agent_id: str) -> dict[str, Any]:
    """Record an agent heartbeat."""
    manager = get_mission_control_manager()
    success = await manager.record_heartbeat(agent_id)

    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent = await manager.get_agent(agent_id)
    return {"agent": agent.to_dict() if agent else None}


# ============================================================================
# Task Endpoints
# ============================================================================


@router.get("/tasks")
async def list_tasks(
    status: str | None = None,
    assignee_id: str | None = None,
    tags: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List tasks with optional filters."""
    manager = get_mission_control_manager()

    # Parse tags from comma-separated string
    tag_list = tags.split(",") if tags else None
    status_enum = TaskStatus(status) if status else None

    tasks = await manager.list_tasks(
        status=status_enum,
        assignee_id=assignee_id,
        tags=tag_list,
    )

    return {
        "tasks": [t.to_dict() for t in tasks[:limit]],
        "count": len(tasks),
    }


@router.post("/tasks")
async def create_task(request: CreateTaskRequest) -> dict[str, Any]:
    """Create a new task, optionally associated with a Deep Work project."""
    manager = get_mission_control_manager()

    task = await manager.create_task(
        title=request.title,
        description=request.description,
        creator_id=request.creator_id,
        priority=TaskPriority(request.priority),
        tags=request.tags,
        assignee_ids=request.assignee_ids if request.assignee_ids else None,
    )

    # Associate with project if project_id is provided
    if request.project_id:
        task.project_id = request.project_id
        await manager._store.save_task(task)
        # Add to project's task_ids list
        project = await manager.get_project(request.project_id)
        if project and task.id not in project.task_ids:
            project.task_ids.append(task.id)
            await manager.update_project(project)

    return {"task": task.to_dict()}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    """Get a task by ID with messages."""
    manager = get_mission_control_manager()
    task = await manager.get_task(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Include messages
    messages = await manager.get_messages_for_task(task_id)

    return {
        "task": task.to_dict(),
        "messages": [m.to_dict() for m in messages],
    }


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, request: UpdateTaskRequest) -> dict[str, Any]:
    """Update a task's details."""
    manager = get_mission_control_manager()
    task = await manager.get_task(task_id)

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if request.title is not None:
        task.title = request.title
    if request.description is not None:
        task.description = request.description
    if request.priority is not None:
        task.priority = TaskPriority(request.priority)
    if request.tags is not None:
        task.tags = request.tags

    # Use update_task_status for status changes (handles timestamps)
    if request.status is not None:
        await manager.update_task_status(task_id, TaskStatus(request.status))
        task = await manager.get_task(task_id)
    else:
        await manager._store.save_task(task)

    return {"task": task.to_dict() if task else None}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str) -> SuccessResponse:
    """Delete a task."""
    manager = get_mission_control_manager()
    deleted = await manager._store.delete_task(task_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Task not found")

    return SuccessResponse(message=f"Task {task_id} deleted")


@router.post("/tasks/{task_id}/assign")
async def assign_task(task_id: str, request: AssignTaskRequest) -> dict[str, Any]:
    """Assign agents to a task."""
    manager = get_mission_control_manager()
    success = await manager.assign_task(task_id, request.agent_ids)

    if not success:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await manager.get_task(task_id)
    return {"task": task.to_dict() if task else None}


@router.post("/tasks/{task_id}/status")
async def update_task_status(task_id: str, request: UpdateTaskStatusRequest) -> dict[str, Any]:
    """Update a task's status.

    Accepts JSON body: {"status": "done", "agent_id": "optional-agent-id"}
    """
    manager = get_mission_control_manager()

    try:
        task_status = TaskStatus(request.status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {request.status}")

    success = await manager.update_task_status(task_id, task_status, request.agent_id)

    if not success:
        raise HTTPException(status_code=404, detail="Task not found")

    task = await manager.get_task(task_id)
    return {"task": task.to_dict() if task else None}


# ============================================================================
# Message Endpoints
# ============================================================================


@router.get("/tasks/{task_id}/messages")
async def get_task_messages(task_id: str, limit: int = 100) -> dict[str, Any]:
    """Get messages for a task."""
    manager = get_mission_control_manager()
    messages = await manager.get_messages_for_task(task_id)

    return {
        "messages": [m.to_dict() for m in messages[:limit]],
        "count": len(messages),
    }


@router.get("/tasks/{task_id}/documents")
async def get_task_documents(task_id: str, limit: int = 100) -> dict[str, Any]:
    """Get documents linked to a task (deliverables, attachments).

    Args:
        task_id: ID of the task
        limit: Maximum documents to return

    Returns:
        List of documents linked to this task
    """
    manager = get_mission_control_manager()

    # Verify task exists
    task = await manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    documents = await manager.list_documents(task_id=task_id)

    return {
        "documents": [d.to_dict() for d in documents[:limit]],
        "count": len(documents),
    }


@router.post("/tasks/{task_id}/messages")
async def post_message(task_id: str, request: PostMessageRequest) -> dict[str, Any]:
    """Post a message to a task thread."""
    manager = get_mission_control_manager()

    # Verify task exists
    task = await manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    message = await manager.post_message(
        task_id=task_id,
        from_agent_id=request.from_agent_id,
        content=request.content,
        attachment_ids=request.attachment_ids if request.attachment_ids else None,
    )

    return {"message": message.to_dict()}


class AttachDocumentRequest(BaseModel):
    """Request to attach a document to a task."""

    document_id: str = Field(..., description="ID of the document to attach")


@router.post("/tasks/{task_id}/attachments")
async def attach_document(task_id: str, request: AttachDocumentRequest) -> dict[str, Any]:
    """Attach an existing document to a task.

    This links the document to the task, making it appear in the task's documents list.

    Args:
        task_id: ID of the task
        request: Contains document_id to attach

    Returns:
        The updated document
    """
    manager = get_mission_control_manager()

    # Verify task exists
    task = await manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Verify document exists
    document = await manager.get_document(request.document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    # Link document to task
    document.task_id = task_id
    await manager._store.save_document(document)

    return {
        "document": document.to_dict(),
        "message": f"Document '{document.title}' attached to task '{task.title}'",
    }


# ============================================================================
# Document Endpoints
# ============================================================================


@router.get("/documents")
async def list_documents(
    type: str | None = None,
    task_id: str | None = None,
    tags: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List documents with optional filters."""
    manager = get_mission_control_manager()
    tag_list = tags.split(",") if tags else None

    documents = await manager.list_documents(
        doc_type=type,
        task_id=task_id,
        tags=tag_list,
    )

    return {
        "documents": [d.to_dict() for d in documents[:limit]],
        "count": len(documents),
    }


@router.post("/documents")
async def create_document(request: CreateDocumentRequest) -> dict[str, Any]:
    """Create a new document."""
    manager = get_mission_control_manager()

    document = await manager.create_document(
        title=request.title,
        content=request.content,
        doc_type=DocumentType(request.type),
        author_id=request.author_id,
        task_id=request.task_id,
        tags=request.tags,
    )

    return {"document": document.to_dict()}


@router.get("/documents/{document_id}")
async def get_document(document_id: str) -> dict[str, Any]:
    """Get a document by ID."""
    manager = get_mission_control_manager()
    document = await manager.get_document(document_id)

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    return {"document": document.to_dict()}


@router.patch("/documents/{document_id}")
async def update_document(document_id: str, request: UpdateDocumentRequest) -> dict[str, Any]:
    """Update a document's content."""
    manager = get_mission_control_manager()

    document = await manager.update_document(
        document_id=document_id,
        content=request.content,
        editor_id=request.editor_id,
    )

    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    return {"document": document.to_dict()}


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str) -> SuccessResponse:
    """Delete a document."""
    manager = get_mission_control_manager()
    deleted = await manager._store.delete_document(document_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")

    return SuccessResponse(message=f"Document {document_id} deleted")


# ============================================================================
# Activity & Stats Endpoints
# ============================================================================


@router.get("/activity")
async def get_activity_feed(
    agent_id: str | None = None,
    task_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Get the activity feed."""
    manager = get_mission_control_manager()

    if agent_id or task_id:
        activities = await manager._store.get_activities(
            agent_id=agent_id,
            task_id=task_id,
            limit=limit,
        )
    else:
        activities = await manager.get_activity_feed(limit)

    return {
        "activities": [a.to_dict() for a in activities],
        "count": len(activities),
    }


@router.get("/stats")
async def get_stats() -> dict[str, Any]:
    """Get Mission Control statistics."""
    manager = get_mission_control_manager()
    stats = await manager.get_stats()
    return {"stats": stats}


@router.get("/standup")
async def get_standup() -> dict[str, Any]:
    """Generate a daily standup summary."""
    manager = get_mission_control_manager()
    standup = await manager.generate_standup()
    return {"standup": standup}


# ============================================================================
# Notification Endpoints
# ============================================================================


@router.get("/notifications")
async def list_notifications(
    agent_id: str | None = None,
    undelivered_only: bool = False,
    unread_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """List notifications."""
    manager = get_mission_control_manager()

    if undelivered_only:
        notifications = await manager.get_undelivered_notifications(agent_id)
    elif agent_id:
        notifications = await manager.get_notifications_for_agent(agent_id, unread_only)
    else:
        notifications = await manager._store.get_notifications_for_agent("", False, limit)
        # Get all notifications (hacky but works for now)
        notifications = list(manager._store._notifications.values())
        notifications.sort(key=lambda n: n.created_at, reverse=True)

    return {
        "notifications": [n.to_dict() for n in notifications[:limit]],
        "count": len(notifications),
    }


@router.post("/notifications/{notification_id}/delivered")
async def mark_delivered(notification_id: str) -> SuccessResponse:
    """Mark a notification as delivered."""
    manager = get_mission_control_manager()
    success = await manager.mark_notification_delivered(notification_id)

    if not success:
        raise HTTPException(status_code=404, detail="Notification not found")

    return SuccessResponse(message="Notification marked as delivered")


@router.post("/notifications/{notification_id}/read")
async def mark_read(notification_id: str) -> SuccessResponse:
    """Mark a notification as read."""
    manager = get_mission_control_manager()
    success = await manager.mark_notification_read(notification_id)

    if not success:
        raise HTTPException(status_code=404, detail="Notification not found")

    return SuccessResponse(message="Notification marked as read")


# ============================================================================
# Project Helpers
# ============================================================================


def _get_project_dir(project_id: str) -> Any:
    """Get the project directory path."""
    from pocketclaw.mission_control.manager import get_project_dir

    return get_project_dir(project_id)


def _count_visible_files(directory: Any) -> int:
    """Count non-hidden files in a directory (non-recursive)."""
    from pathlib import Path

    d = Path(directory)
    if not d.exists() or not d.is_dir():
        return 0
    return sum(1 for f in d.iterdir() if not f.name.startswith("."))


def _enrich_project_dict(project_dict: dict) -> dict:
    """Add folder_path and file_count to a project dict."""
    project_id = project_dict.get("id", "")
    project_dir = _get_project_dir(project_id)
    project_dict["folder_path"] = str(project_dir)
    project_dict["file_count"] = _count_visible_files(project_dir)
    return project_dict


# ============================================================================
# Project Endpoints (Deep Work)
# ============================================================================


class CreateProjectRequest(BaseModel):
    """Request to create a new project."""

    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="")
    tags: list[str] = Field(default_factory=list)


class UpdateProjectRequest(BaseModel):
    """Request to update a project."""

    title: str | None = None
    description: str | None = None
    status: str | None = None
    tags: list[str] | None = None


@router.post("/projects")
async def create_project(request: CreateProjectRequest) -> dict[str, Any]:
    """Create a new project."""
    manager = get_mission_control_manager()

    project = await manager.create_project(
        title=request.title,
        description=request.description,
        tags=request.tags,
    )

    return {"project": project.to_dict()}


@router.get("/projects")
async def list_projects(status: str | None = None, limit: int = 100) -> dict[str, Any]:
    """List projects, optionally filtered by status."""
    manager = get_mission_control_manager()
    projects = await manager.list_projects(status)

    enriched = [_enrich_project_dict(p.to_dict()) for p in projects[:limit]]

    return {
        "projects": enriched,
        "count": len(projects),
    }


@router.get("/projects/{project_id}")
async def get_project(project_id: str) -> dict[str, Any]:
    """Get a project by ID, including its tasks and progress."""
    manager = get_mission_control_manager()
    project = await manager.get_project(project_id)

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    tasks = await manager.get_project_tasks(project_id)
    progress = await manager.get_project_progress(project_id)

    return {
        "project": _enrich_project_dict(project.to_dict()),
        "tasks": [t.to_dict() for t in tasks],
        "progress": progress,
    }


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, request: UpdateProjectRequest) -> dict[str, Any]:
    """Update a project's details."""
    from pocketclaw.deep_work.models import ProjectStatus

    manager = get_mission_control_manager()
    project = await manager.get_project(project_id)

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    if request.title is not None:
        project.title = request.title
    if request.description is not None:
        project.description = request.description
    if request.status is not None:
        project.status = ProjectStatus(request.status)
    if request.tags is not None:
        project.tags = request.tags

    await manager.update_project(project)
    return {"project": project.to_dict()}


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str) -> SuccessResponse:
    """Delete a project."""
    manager = get_mission_control_manager()
    deleted = await manager.delete_project(project_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Project not found")

    return SuccessResponse(message=f"Project {project_id} deleted")


@router.post("/projects/{project_id}/approve")
async def approve_project(project_id: str) -> dict[str, Any]:
    """Approve a project (simple status change).

    For full orchestration with task dispatch, use the Deep Work API
    at POST /api/deep-work/projects/{id}/approve instead.
    """
    from pocketclaw.deep_work.models import ProjectStatus

    manager = get_mission_control_manager()
    project = await manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project.status = ProjectStatus.APPROVED
    await manager.update_project(project)
    return {"project": project.to_dict()}


@router.post("/projects/{project_id}/pause")
async def pause_project(project_id: str) -> dict[str, Any]:
    """Pause a project (simple status change).

    For full orchestration with task stopping, use the Deep Work API
    at POST /api/deep-work/projects/{id}/pause instead.
    """
    from pocketclaw.deep_work.models import ProjectStatus

    manager = get_mission_control_manager()
    project = await manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project.status = ProjectStatus.PAUSED
    await manager.update_project(project)
    return {"project": project.to_dict()}


@router.post("/projects/{project_id}/resume")
async def resume_project(project_id: str) -> dict[str, Any]:
    """Resume a project (simple status change).

    For full orchestration with task dispatch, use the Deep Work API
    at POST /api/deep-work/projects/{id}/resume instead.
    """
    from pocketclaw.deep_work.models import ProjectStatus

    manager = get_mission_control_manager()
    project = await manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project.status = ProjectStatus.EXECUTING
    await manager.update_project(project)
    return {"project": project.to_dict()}


# ============================================================================
# Task Execution Endpoints
# ============================================================================


class RunTaskRequest(BaseModel):
    """Request to run a task."""

    agent_id: str = Field(
        ...,
        description="ID of the agent to execute the task",
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )


@router.post("/tasks/{task_id}/run")
async def run_task(task_id: str, request: RunTaskRequest) -> dict[str, Any]:
    """Start executing a task with the specified agent.

    The task runs in the background. Execution events are streamed via WebSocket:
    - mc_task_started: When execution begins
    - mc_task_output: For each output chunk from the agent
    - mc_task_completed: When execution ends (success/error/stopped)

    Args:
        task_id: ID of the task to execute
        request: Contains agent_id to execute the task

    Returns:
        Status information about the execution start
    """
    from pocketclaw.mission_control.executor import get_mc_task_executor

    manager = get_mission_control_manager()
    executor = get_mc_task_executor()

    # Validate task exists
    task = await manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Validate agent exists
    agent = await manager.get_agent(request.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if task is already running
    if executor.is_task_running(task_id):
        raise HTTPException(status_code=409, detail="Task is already running")

    # Start execution in background
    await executor.execute_task_background(task_id, request.agent_id)

    return {
        "status": "started",
        "task_id": task_id,
        "agent_id": request.agent_id,
        "agent_name": agent.name,
        "message": f"Task '{task.title}' execution started with agent {agent.name}",
    }


@router.post("/tasks/{task_id}/stop")
async def stop_task(task_id: str) -> dict[str, Any]:
    """Stop a running task execution.

    Args:
        task_id: ID of the task to stop

    Returns:
        Status information about the stop operation
    """
    from pocketclaw.mission_control.executor import get_mc_task_executor

    executor = get_mc_task_executor()

    if not executor.is_task_running(task_id):
        raise HTTPException(status_code=404, detail="Task is not currently running")

    success = await executor.stop_task(task_id)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to stop task")

    return {
        "status": "stopped",
        "task_id": task_id,
        "message": "Task execution stopped",
    }


@router.get("/tasks/running")
async def get_running_tasks() -> dict[str, Any]:
    """Get list of currently running task executions.

    Returns:
        List of running task IDs and their details
    """
    from pocketclaw.mission_control.executor import get_mc_task_executor

    manager = get_mission_control_manager()
    executor = get_mc_task_executor()

    running_ids = executor.get_running_tasks()

    # Fetch task details for each running task
    running_tasks = []
    for task_id in running_ids:
        task = await manager.get_task(task_id)
        if task:
            running_tasks.append(
                {
                    "task_id": task_id,
                    "title": task.title,
                    "status": task.status.value,
                    "assignee_ids": task.assignee_ids,
                }
            )

    return {
        "running_tasks": running_tasks,
        "count": len(running_tasks),
    }
