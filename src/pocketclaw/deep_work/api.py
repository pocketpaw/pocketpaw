# Deep Work API endpoints.
# Created: 2026-02-12
# Updated: 2026-02-12 — Added 'none' option to research_depth (skip research entirely).
#   Added execution_levels + task_level_map to get_plan().
#   Added POST /projects/{id}/tasks/{task_id}/skip endpoint for skipping tasks.
#   Added research_depth parameter to start endpoint.
#
# FastAPI router for Deep Work orchestration:
#   POST /start                               — submit project (natural language)
#   GET  /projects/{id}/plan                  — get plan with execution_levels
#   POST /projects/{id}/approve               — approve plan, start execution
#   POST /projects/{id}/pause                 — pause execution
#   POST /projects/{id}/resume                — resume execution
#   POST /projects/{id}/tasks/{tid}/skip      — skip a task
#
# Mount: app.include_router(deep_work_router, prefix="/api/deep-work")

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Deep Work"])


class StartDeepWorkRequest(BaseModel):
    """Request body for starting a Deep Work project."""

    description: str = Field(
        ..., min_length=10, max_length=5000, description="Natural language project description"
    )
    research_depth: str = Field(
        default="standard",
        description="Research thoroughness: 'none' (skip entirely), 'quick', 'standard', or 'deep'",
    )


@router.post("/start")
async def start_deep_work(request: StartDeepWorkRequest) -> dict[str, Any]:
    """Submit a new project for Deep Work planning.

    Returns the project immediately in PLANNING status and runs the
    planner in the background. Frontend tracks progress via WebSocket
    events (dw_planning_phase, dw_planning_complete).
    """
    from pocketclaw.deep_work import get_deep_work_session
    from pocketclaw.deep_work.models import ProjectStatus
    from pocketclaw.mission_control.manager import get_mission_control_manager

    manager = get_mission_control_manager()

    # Create project immediately so we can return the ID
    project = await manager.create_project(
        title=request.description[:80],
        description=request.description,
        creator_id="human",
    )
    project.status = ProjectStatus.PLANNING
    await manager.update_project(project)

    # Run planning in background — frontend tracks via WebSocket events
    async def _plan_in_background():
        session = get_deep_work_session()
        try:
            await session.plan_existing_project(
                project.id,
                request.description,
                research_depth=request.research_depth,
            )
        except Exception as e:
            logger.exception(f"Background planning failed for {project.id}: {e}")

    asyncio.create_task(_plan_in_background())

    return {"success": True, "project": project.to_dict()}


@router.get("/projects/{project_id}/plan")
async def get_plan(project_id: str) -> dict[str, Any]:
    """Get the generated plan for a project.

    Returns project details, tasks, progress, PRD document, and execution_levels
    (task IDs grouped by dependency level for parallel execution).
    """
    from pocketclaw.deep_work.scheduler import DependencyScheduler
    from pocketclaw.mission_control.manager import get_mission_control_manager

    manager = get_mission_control_manager()
    project = await manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    tasks = await manager.get_project_tasks(project_id)
    progress = await manager.get_project_progress(project_id)

    # Compute execution levels from dependency graph
    execution_levels = DependencyScheduler.get_execution_order(tasks)
    task_level_map = {}
    for level_idx, level_ids in enumerate(execution_levels):
        for tid in level_ids:
            task_level_map[tid] = level_idx

    # Get PRD document if available
    prd = None
    if project.prd_document_id:
        prd_doc = await manager.get_document(project.prd_document_id)
        if prd_doc:
            prd = prd_doc.to_dict()

    return {
        "project": project.to_dict(),
        "tasks": [t.to_dict() for t in tasks],
        "progress": progress,
        "prd": prd,
        "execution_levels": execution_levels,
        "task_level_map": task_level_map,
    }


@router.post("/projects/{project_id}/approve")
async def approve_project(project_id: str) -> dict[str, Any]:
    """Approve a project plan and start execution."""
    from pocketclaw.deep_work import approve_project as _approve

    try:
        project = await _approve(project_id)
        return {"success": True, "project": project.to_dict()}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception(f"Approve failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/pause")
async def pause_project(project_id: str) -> dict[str, Any]:
    """Pause project execution."""
    from pocketclaw.deep_work import pause_project as _pause

    try:
        project = await _pause(project_id)
        return {"success": True, "project": project.to_dict()}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception(f"Pause failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/resume")
async def resume_project(project_id: str) -> dict[str, Any]:
    """Resume a paused project."""
    from pocketclaw.deep_work import resume_project as _resume

    try:
        project = await _resume(project_id)
        return {"success": True, "project": project.to_dict()}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception(f"Resume failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/tasks/{task_id}/skip")
async def skip_task(project_id: str, task_id: str) -> dict[str, Any]:
    """Skip a task without running it, unblocking dependents.

    Sets task status to SKIPPED with completed_at timestamp, then
    cascades unblocking via the scheduler.
    """
    from pocketclaw.deep_work import get_deep_work_session
    from pocketclaw.mission_control.manager import get_mission_control_manager
    from pocketclaw.mission_control.models import TaskStatus, now_iso

    manager = get_mission_control_manager()

    task = await manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.project_id != project_id:
        raise HTTPException(status_code=400, detail="Task does not belong to this project")
    if task.status in (TaskStatus.DONE, TaskStatus.SKIPPED, TaskStatus.IN_PROGRESS):
        raise HTTPException(
            status_code=400, detail=f"Cannot skip task with status '{task.status.value}'"
        )

    # Set SKIPPED status
    task.status = TaskStatus.SKIPPED
    task.completed_at = now_iso()
    task.updated_at = now_iso()
    await manager.save_task(task)

    # Cascade: unblock dependents and check project completion
    try:
        session = get_deep_work_session()
        await session.scheduler.on_task_completed(task_id)
    except Exception as e:
        logger.warning(f"Scheduler cascade after skip failed: {e}")

    # Return updated task and progress
    progress = await manager.get_project_progress(project_id)

    return {
        "success": True,
        "task": task.to_dict(),
        "progress": progress,
    }
