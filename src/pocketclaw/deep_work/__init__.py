# Deep Work — AI project orchestration layer for PocketPaw.
# Created: 2026-02-12
# Updated: 2026-02-12 — Added executor integration, public API functions.
#   Added research_depth parameter to start_deep_work().
#
# Provides a singleton DeepWorkSession and convenience functions for
# starting and managing Deep Work projects.
#
# Public API:
#   get_deep_work_session() -> DeepWorkSession
#   reset_deep_work_session() -> None
#   start_deep_work(user_input) -> Project
#   approve_project(project_id) -> Project
#   pause_project(project_id) -> Project
#   resume_project(project_id) -> Project

import logging

from pocketclaw.deep_work.models import (
    AgentSpec,
    PlannerResult,
    Project,
    ProjectStatus,
    TaskSpec,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AgentSpec",
    "PlannerResult",
    "Project",
    "ProjectStatus",
    "TaskSpec",
    "get_deep_work_session",
    "reset_deep_work_session",
    "start_deep_work",
    "approve_project",
    "pause_project",
    "resume_project",
    "recover_interrupted_projects",
]

_session_instance = None


def get_deep_work_session():
    """Get or create the singleton DeepWorkSession.

    Lazily constructs all dependencies (manager, executor, planner,
    scheduler, human_router) on first call. Also subscribes to the
    MessageBus for task completion events.
    """
    global _session_instance
    if _session_instance is not None:
        return _session_instance

    from pocketclaw.deep_work.session import DeepWorkSession
    from pocketclaw.mission_control.executor import get_mc_task_executor
    from pocketclaw.mission_control.manager import get_mission_control_manager

    manager = get_mission_control_manager()
    executor = get_mc_task_executor()

    session = DeepWorkSession(manager, executor)
    session.subscribe_to_bus()

    _session_instance = session
    return session


def reset_deep_work_session() -> None:
    """Reset the singleton session (for testing)."""
    global _session_instance
    _session_instance = None


async def start_deep_work(user_input: str, research_depth: str = "standard") -> Project:
    """Submit a new project for Deep Work planning.

    Args:
        user_input: Natural language project description.
        research_depth: "none" (skip), "quick", "standard", or "deep".

    Returns:
        The created Project (status=AWAITING_APPROVAL after planning).
    """
    session = get_deep_work_session()
    return await session.start(user_input, research_depth=research_depth)


async def approve_project(project_id: str) -> Project:
    """Approve a project plan and start execution.

    Args:
        project_id: ID of the project to approve.

    Returns:
        The updated Project (status=EXECUTING).
    """
    session = get_deep_work_session()
    return await session.approve(project_id)


async def pause_project(project_id: str) -> Project:
    """Pause project execution.

    Args:
        project_id: ID of the project to pause.

    Returns:
        The updated Project (status=PAUSED).
    """
    session = get_deep_work_session()
    return await session.pause(project_id)


async def resume_project(project_id: str) -> Project:
    """Resume a paused project.

    Args:
        project_id: ID of the project to resume.

    Returns:
        The updated Project (status=EXECUTING).
    """
    session = get_deep_work_session()
    return await session.resume(project_id)


async def recover_interrupted_projects() -> int:
    """Recover projects interrupted by a server restart.

    Should be called once on application startup.

    Returns:
        Number of projects recovered.
    """
    session = get_deep_work_session()
    return await session.recover_interrupted_projects()
