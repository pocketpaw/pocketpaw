# Deep Work API endpoints.
# Created: 2026-02-12
# Updated: 2026-02-26 — Deep Work v2: Added cancel and retry endpoints.
#   POST /projects/{id}/cancel — cancel project, stop all tasks
#   POST /projects/{id}/tasks/{tid}/retry — manual retry of a failed task
# Updated: 2026-02-18 — Added POST /parse-goal endpoint.
# Updated: 2026-02-16 — Enrich project dict with folder_path and file_count.
#
# FastAPI router for Deep Work orchestration:
#   POST /parse-goal                          — analyze goal (domain, complexity)
#   POST /start                               — submit project (natural language)
#   GET  /projects/{id}/plan                  — get plan with execution_levels
#   POST /projects/{id}/approve               — approve plan, start execution
#   POST /projects/{id}/pause                 — pause execution
#   POST /projects/{id}/resume                — resume execution
#   POST /projects/{id}/cancel                — cancel project
#   POST /projects/{id}/tasks/{tid}/skip      — skip a task
#   POST /projects/{id}/tasks/{tid}/retry     — retry a failed task
#
# Mount: app.include_router(deep_work_router, prefix="/api/deep-work")

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Deep Work"])


class ParseGoalRequest(BaseModel):
    """Request body for goal analysis."""

    description: str = Field(
        ..., min_length=10, max_length=5000, description="Natural language goal description"
    )


class StartDeepWorkRequest(BaseModel):
    """Request body for starting a Deep Work project."""

    description: str = Field(
        ..., min_length=10, max_length=5000, description="Natural language project description"
    )
    research_depth: str = Field(
        default="auto",
        description=(
            "Research thoroughness: 'auto' (use goal parser suggestion), "
            "'none', 'quick', 'standard', or 'deep'"
        ),
    )
    goal_analysis: dict | None = Field(
        default=None,
        description="Pre-parsed goal analysis dict (from /parse-goal). Skips re-parsing.",
    )


def _enrich_project_dict(project_dict: dict) -> dict:
    """Add folder_path and file_count to a project dict for frontend output panel."""
    from pathlib import Path

    from pocketpaw.mission_control.manager import get_project_dir

    project_id = project_dict.get("id", "")
    project_dir = get_project_dir(project_id)
    project_dict["folder_path"] = str(project_dir)
    d = Path(project_dir)
    project_dict["file_count"] = (
        sum(1 for f in d.iterdir() if not f.name.startswith("."))
        if d.exists() and d.is_dir()
        else 0
    )
    return project_dict


@router.post("/parse-goal")
async def parse_goal(request: ParseGoalRequest) -> dict[str, Any]:
    """Analyze a user's goal and return structured analysis.

    Returns domain detection, complexity estimation, AI/human roles,
    and clarification questions. This is a preview step — the user
    can review the analysis before starting planning.
    """
    from pocketpaw.deep_work.goal_parser import GoalParser

    try:
        parser = GoalParser()
        analysis = await parser.parse(request.description)
        return {"success": True, "goal_analysis": analysis.to_dict()}
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.exception(f"Goal parsing failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/start")
async def start_deep_work(request: StartDeepWorkRequest) -> dict[str, Any]:
    """Submit a new project for Deep Work planning.

    Returns the project immediately in PLANNING status and runs the
    planner in the background. Frontend tracks progress via WebSocket
    events (dw_planning_phase, dw_planning_complete).
    """
    from pocketpaw.deep_work import get_deep_work_session
    from pocketpaw.deep_work.models import ProjectStatus
    from pocketpaw.mission_control.manager import get_mission_control_manager

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
                goal_analysis=request.goal_analysis,
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
    from pocketpaw.deep_work.scheduler import DependencyScheduler
    from pocketpaw.mission_control.manager import get_mission_control_manager

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

    project_dict = _enrich_project_dict(project.to_dict())

    # Include goal analysis from project metadata (if available)
    goal_analysis = project.metadata.get("goal_analysis")

    return {
        "project": project_dict,
        "tasks": [t.to_dict() for t in tasks],
        "progress": progress,
        "prd": prd,
        "execution_levels": execution_levels,
        "task_level_map": task_level_map,
        "goal_analysis": goal_analysis,
    }


@router.post("/projects/{project_id}/approve")
async def approve_project(project_id: str) -> dict[str, Any]:
    """Approve a project plan and start execution."""
    from pocketpaw.deep_work import approve_project as _approve

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
    from pocketpaw.deep_work import pause_project as _pause

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
    from pocketpaw.deep_work import resume_project as _resume

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
    from pocketpaw.deep_work import get_deep_work_session
    from pocketpaw.mission_control.manager import get_mission_control_manager
    from pocketpaw.mission_control.models import TaskStatus, now_iso

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


@router.post("/projects/{project_id}/cancel")
async def cancel_project(project_id: str) -> dict[str, Any]:
    """Cancel a project — stop all tasks and mark as cancelled."""
    from pocketpaw.deep_work import cancel_project as _cancel

    try:
        project = await _cancel(project_id)
        return {"success": True, "project": project.to_dict()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Cancel failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/projects/{project_id}/tasks/{task_id}/retry")
async def retry_task(project_id: str, task_id: str) -> dict[str, Any]:
    """Manually retry a failed/blocked task.

    Resets the task status to ASSIGNED and re-dispatches it.
    Increments retry_count regardless of max_retries (manual override).
    """
    from pocketpaw.deep_work import get_deep_work_session
    from pocketpaw.mission_control.manager import get_mission_control_manager
    from pocketpaw.mission_control.models import TaskStatus, now_iso

    manager = get_mission_control_manager()

    task = await manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.project_id != project_id:
        raise HTTPException(status_code=400, detail="Task does not belong to this project")
    if task.status not in (TaskStatus.BLOCKED,):
        raise HTTPException(
            status_code=400,
            detail=f"Can only retry tasks with status 'blocked', got '{task.status.value}'",
        )

    # Reset task for retry
    task.status = TaskStatus.ASSIGNED
    task.retry_count += 1
    task.error_message = None
    task.updated_at = now_iso()
    await manager.save_task(task)

    # Re-dispatch via scheduler
    try:
        session = get_deep_work_session()
        await session.scheduler._dispatch_task(task)
    except Exception as e:
        logger.warning(f"Re-dispatch after manual retry failed: {e}")
        raise HTTPException(status_code=500, detail=f"Retry dispatch failed: {e}")

    progress = await manager.get_project_progress(project_id)

    return {
        "success": True,
        "task": task.to_dict(),
        "progress": progress,
    }
