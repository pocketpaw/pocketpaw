# Deep Work Session — project lifecycle orchestrator.
# Created: 2026-02-12
# Updated: 2026-02-12 — Added executor integration for pause/stop, made
#   planner/scheduler/human_router optional with sensible defaults,
#   improved _assign_tasks_to_agents to use key_to_id mapping.
#   Added research_depth parameter to start() for controlling planner depth.
#
# Ties together the Planner, DependencyScheduler, MCTaskExecutor, and
# HumanTaskRouter into a single class that manages a Deep Work project
# from user input through planning, approval, execution, and completion.
#
# Public API:
#   session.start(user_input) -> Project   (create + plan + await approval)
#   session.approve(project_id) -> Project (kick off ready tasks)
#   session.pause(project_id) -> Project   (stop running tasks)
#   session.resume(project_id) -> Project  (resume dispatching)

import logging
from typing import Any

from pocketclaw.deep_work.human_tasks import HumanTaskRouter
from pocketclaw.deep_work.models import Project, ProjectStatus
from pocketclaw.deep_work.planner import PlannerAgent
from pocketclaw.deep_work.scheduler import DependencyScheduler
from pocketclaw.mission_control.manager import MissionControlManager
from pocketclaw.mission_control.models import (
    DocumentType,
    TaskPriority,
    TaskStatus,
    now_iso,
)

logger = logging.getLogger(__name__)


class DeepWorkSession:
    """Manages a Deep Work project from submission to completion.

    Orchestrates:
      - PlannerAgent: decomposes goal into tasks and team
      - DependencyScheduler: dispatches tasks as blockers resolve
      - MCTaskExecutor: runs agent tasks in isolated routers
      - HumanTaskRouter: pushes human tasks to messaging channels
      - MissionControlManager: persists all objects

    The session does NOT subscribe to MessageBus events in __init__.
    Call subscribe_to_bus() explicitly at dashboard startup if desired.
    """

    def __init__(
        self,
        manager: MissionControlManager,
        executor,
        planner: PlannerAgent | None = None,
        scheduler: DependencyScheduler | None = None,
        human_router: HumanTaskRouter | None = None,
    ):
        self.manager = manager
        self.executor = executor
        self.planner = planner or PlannerAgent(manager)
        self.human_router = human_router or HumanTaskRouter()
        self.scheduler = scheduler or DependencyScheduler(manager, executor, self.human_router)
        self._subscribed = False

        # Wire direct executor → scheduler callback for reliable cascade dispatch.
        # This bypasses MessageBus so task completion always triggers dependent
        # task dispatch even if the bus drops an event.
        executor._on_task_done_callback = self.scheduler.on_task_completed

    def subscribe_to_bus(self) -> None:
        """Subscribe to MessageBus for task completion events.

        Call this once after constructing the session (e.g. during
        dashboard startup). Safe to call multiple times — only subscribes
        once.
        """
        if self._subscribed:
            return
        try:
            from pocketclaw.bus import get_message_bus

            bus = get_message_bus()
            bus.subscribe_system(self._on_system_event)
            self._subscribed = True
            logger.info("DeepWorkSession subscribed to MessageBus")
        except Exception as e:
            logger.warning(f"Could not subscribe to MessageBus: {e}")

    # =========================================================================
    # Startup recovery
    # =========================================================================

    async def recover_interrupted_projects(self) -> int:
        """Recover projects interrupted by a server restart.

        Called once on application startup. Handles:
        - PLANNING projects: marked as FAILED (planning interrupted).
        - EXECUTING projects: stuck IN_PROGRESS tasks reset to ASSIGNED,
          then ready tasks are re-dispatched.

        Returns:
            Number of projects recovered.
        """
        import asyncio

        recovered = 0
        projects = await self.manager.list_projects()

        for project in projects:
            if project.status == ProjectStatus.PLANNING:
                # Planning was interrupted — mark as failed
                project.status = ProjectStatus.FAILED
                project.metadata["error"] = "Planning interrupted by server restart"
                await self.manager.update_project(project)
                logger.info(f"Marked interrupted planning project as failed: {project.title}")
                self._broadcast_planning_complete(project)
                recovered += 1

            elif project.status == ProjectStatus.EXECUTING:
                # Reset stuck IN_PROGRESS tasks to ASSIGNED
                tasks = await self.manager.get_project_tasks(project.id)
                reset_count = 0
                for task in tasks:
                    if task.status == TaskStatus.IN_PROGRESS:
                        # Not actually running (executor state is gone after restart)
                        if not self.executor.is_task_running(task.id):
                            task.status = TaskStatus.ASSIGNED
                            task.updated_at = now_iso()
                            await self.manager.save_task(task)
                            reset_count += 1

                if reset_count > 0:
                    logger.info(f"Reset {reset_count} stuck tasks for project: {project.title}")
                    # Re-dispatch ready tasks
                    ready = await self.scheduler.get_ready_tasks(project.id)
                    if ready:
                        await asyncio.gather(*(self.scheduler._dispatch_task(t) for t in ready))
                    recovered += 1

        if recovered:
            logger.info(f"Recovered {recovered} interrupted project(s)")
        return recovered

    # =========================================================================
    # Public lifecycle methods
    # =========================================================================

    async def start(self, user_input: str, research_depth: str = "standard") -> Project:
        """Create a project, run the planner, and prepare for approval.

        Flow:
          1. Create Project in DRAFT
          2. Transition to PLANNING, run PlannerAgent
          3. Save PRD as Document
          4. Validate dependency graph
          5. Create MC Tasks from PlannerResult
          6. Create agent team profiles
          7. Auto-assign tasks to agents by specialty
          8. Transition to AWAITING_APPROVAL
          9. Notify user that plan is ready

        Args:
            user_input: Natural language project description.
            research_depth: How thorough to research — "quick", "standard",
                or "deep".

        Returns:
            The created Project (status=AWAITING_APPROVAL on success,
            FAILED on planning/graph errors).
        """
        # 1. Create project
        project = await self.manager.create_project(
            title=user_input[:80],
            description=user_input,
            creator_id="human",
        )

        # 2. Run planning on the new project
        return await self.plan_existing_project(
            project.id, user_input, research_depth=research_depth
        )

    async def plan_existing_project(
        self, project_id: str, user_input: str, research_depth: str = "standard"
    ) -> Project:
        """Run planner on an already-created project.

        Called by start() or by the async API endpoint. Broadcasts a
        dw_planning_complete event when done (success or failure).

        Args:
            project_id: ID of the project to plan.
            user_input: Natural language project description.
            research_depth: How thorough to research — "none", "quick",
                "standard", or "deep".

        Returns:
            The updated Project.
        """
        project = await self.manager.get_project(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

        try:
            # Plan
            project.status = ProjectStatus.PLANNING
            await self.manager.update_project(project)

            result = await self.planner.plan(
                user_input, project_id=project.id, research_depth=research_depth
            )

            # Set project title from PRD (first heading or fallback)
            title = _extract_title(result.prd_content) or user_input[:80]
            project.title = title

            # Save PRD as Document
            if result.prd_content:
                prd_doc = await self.manager.create_document(
                    title=f"PRD: {title}",
                    content=result.prd_content,
                    doc_type=DocumentType.PROTOCOL,
                    tags=["prd", "deep-work", "auto-generated"],
                )
                project.prd_document_id = prd_doc.id

            # Validate dependency graph
            all_tasks = result.tasks + result.human_tasks
            is_valid, error_msg = DependencyScheduler.validate_graph(all_tasks)
            if not is_valid:
                project.status = ProjectStatus.FAILED
                project.metadata["error"] = f"Invalid dependency graph: {error_msg}"
                await self.manager.update_project(project)
                self._broadcast_planning_complete(project)
                return project

            # Handle empty task list
            if not all_tasks:
                project.status = ProjectStatus.FAILED
                project.metadata["error"] = "Planner produced no tasks"
                await self.manager.update_project(project)
                self._broadcast_planning_complete(project)
                return project

            # Create MC Tasks
            key_to_id = await self._materialize_tasks(project, all_tasks)

            # Create agent team
            for agent_spec in result.team_recommendation:
                existing = await self.manager.get_agent_by_name(agent_spec.name)
                if existing:
                    project.team_agent_ids.append(existing.id)
                else:
                    agent = await self.manager.create_agent(
                        name=agent_spec.name,
                        role=agent_spec.role,
                        description=agent_spec.description,
                        specialties=agent_spec.specialties,
                        backend=agent_spec.backend,
                    )
                    project.team_agent_ids.append(agent.id)

            # Auto-assign tasks to agents
            await self._assign_tasks_to_agents(project, result, key_to_id)

            # Transition to AWAITING_APPROVAL
            project.status = ProjectStatus.AWAITING_APPROVAL
            await self.manager.update_project(project)

            # Notify
            task_count = len(all_tasks)
            await self.human_router.notify_plan_ready(
                project,
                task_count=task_count,
                estimated_minutes=result.estimated_total_minutes,
            )

            self._broadcast_planning_complete(project)

        except Exception as e:
            logger.exception(f"Planning failed for project {project.id}: {e}")
            project.status = ProjectStatus.FAILED
            project.metadata["error"] = str(e)
            await self.manager.update_project(project)
            self._broadcast_planning_complete(project)
            raise

        return project

    async def approve(self, project_id: str) -> Project:
        """User approves the plan — start executing ready tasks.

        Args:
            project_id: ID of the project to approve.

        Returns:
            The updated Project (status=EXECUTING).

        Raises:
            ValueError: If project not found.
        """
        import asyncio

        project = await self.manager.get_project(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")
        if project.status != ProjectStatus.AWAITING_APPROVAL:
            raise ValueError(f"Cannot approve project with status '{project.status.value}'")

        project.status = ProjectStatus.EXECUTING
        project.started_at = now_iso()
        await self.manager.update_project(project)

        # Kick off tasks with no blockers — dispatch concurrently
        ready = await self.scheduler.get_ready_tasks(project_id)
        if ready:
            await asyncio.gather(*(self.scheduler._dispatch_task(t) for t in ready))

        return project

    async def pause(self, project_id: str) -> Project:
        """Pause execution of a project — stop all running tasks.

        Args:
            project_id: ID of the project to pause.

        Returns:
            The updated Project (status=PAUSED).

        Raises:
            ValueError: If project not found.
        """
        project = await self.manager.get_project(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")
        if project.status != ProjectStatus.EXECUTING:
            raise ValueError(f"Cannot pause project with status '{project.status.value}'")

        # Stop all running tasks for this project
        for task_id in project.task_ids:
            if self.executor.is_task_running(task_id):
                await self.executor.stop_task(task_id)

        project.status = ProjectStatus.PAUSED
        await self.manager.update_project(project)
        logger.info(f"Project paused: {project.title}")
        return project

    async def resume(self, project_id: str) -> Project:
        """Resume a paused project.

        Sets status back to EXECUTING and dispatches any ready tasks.

        Args:
            project_id: ID of the project to resume.

        Returns:
            The updated Project (status=EXECUTING).

        Raises:
            ValueError: If project not found.
        """
        import asyncio

        project = await self.manager.get_project(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")
        if project.status != ProjectStatus.PAUSED:
            raise ValueError(f"Cannot resume project with status '{project.status.value}'")

        project.status = ProjectStatus.EXECUTING
        await self.manager.update_project(project)

        ready = await self.scheduler.get_ready_tasks(project_id)
        if ready:
            await asyncio.gather(*(self.scheduler._dispatch_task(t) for t in ready))

        logger.info(f"Project resumed: {project.title}")
        return project

    # =========================================================================
    # MessageBus event handler
    # =========================================================================

    async def _on_system_event(self, event: Any) -> None:
        """Handle MessageBus SystemEvents.

        Note: Task completion → scheduler cascade is now handled directly
        via executor._on_task_done_callback for reliability. This handler
        remains for future event types.
        """
        pass

    # =========================================================================
    # Broadcasting helpers
    # =========================================================================

    def _broadcast_planning_complete(self, project: Project) -> None:
        """Broadcast a planning completion event for the frontend.

        Sends dw_planning_complete with the project status so the frontend
        knows to stop the spinner and reload the plan.
        """
        try:
            import asyncio

            from pocketclaw.bus import get_message_bus
            from pocketclaw.bus.events import SystemEvent

            bus = get_message_bus()
            loop = asyncio.get_running_loop()
            loop.create_task(
                bus.publish_system(
                    SystemEvent(
                        event_type="dw_planning_complete",
                        data={
                            "project_id": project.id,
                            "status": project.status.value
                            if hasattr(project.status, "value")
                            else str(project.status),
                            "title": project.title,
                            "error": project.metadata.get("error"),
                        },
                    )
                )
            )
        except Exception:
            pass  # Best effort

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _materialize_tasks(self, project: Project, task_specs: list) -> dict[str, str]:
        """Create MC Tasks from TaskSpecs and resolve dependency keys to IDs.

        Also sets inverse blocks on upstream tasks.

        Args:
            project: The project these tasks belong to.
            task_specs: List of TaskSpec objects from the planner.

        Returns:
            Mapping of spec key -> MC Task ID.
        """
        key_to_id: dict[str, str] = {}

        for spec in task_specs:
            priority = TaskPriority.MEDIUM
            try:
                priority = TaskPriority(spec.priority)
            except ValueError:
                pass

            task = await self.manager.create_task(
                title=spec.title,
                description=spec.description,
                priority=priority,
                tags=spec.tags,
            )

            # Set deep work fields
            task.project_id = project.id
            task.task_type = spec.task_type
            task.estimated_minutes = spec.estimated_minutes
            task.blocked_by = [key_to_id[k] for k in spec.blocked_by_keys if k in key_to_id]

            # Set inverse blocks on upstream tasks
            for dep_key in spec.blocked_by_keys:
                dep_id = key_to_id.get(dep_key)
                if dep_id:
                    dep_task = await self.manager.get_task(dep_id)
                    if dep_task and task.id not in dep_task.blocks:
                        dep_task.blocks.append(task.id)
                        await self.manager.save_task(dep_task)

            key_to_id[spec.key] = task.id
            project.task_ids.append(task.id)

            # Re-save the task with deep work fields
            await self.manager.save_task(task)

        return key_to_id

    async def _assign_tasks_to_agents(
        self,
        project: Project,
        planner_result,
        key_to_id: dict[str, str],
    ) -> None:
        """Match tasks to agents by required_specialties.

        Uses key_to_id mapping for reliable task lookup instead of
        title matching.

        Args:
            project: The project containing tasks and agents.
            planner_result: PlannerResult with task specs.
            key_to_id: Mapping of spec key -> MC Task ID.
        """
        # Build agent lookup: agent_id -> set of specialties
        agents: list[tuple[str, set[str]]] = []
        for agent_id in project.team_agent_ids:
            agent = await self.manager.get_agent(agent_id)
            if agent:
                agents.append((agent.id, set(agent.specialties)))

        # Combine all task specs
        all_task_specs = planner_result.tasks + planner_result.human_tasks

        for spec in all_task_specs:
            if spec.task_type != "agent":
                continue  # Only auto-assign agent tasks

            task_id = key_to_id.get(spec.key)
            if not task_id:
                continue

            required = set(spec.required_specialties)
            best_agent_id = None
            best_overlap = -1

            for agent_id, specialties in agents:
                overlap = len(required & specialties)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_agent_id = agent_id

            if best_agent_id:
                await self.manager.assign_task(task_id, [best_agent_id])


def _extract_title(prd_content: str) -> str:
    """Extract a project title from the PRD content.

    Looks for the first markdown heading or falls back to the first line.
    """
    if not prd_content:
        return ""
    for line in prd_content.strip().splitlines():
        line = line.strip()
        if line.startswith("#"):
            # Remove heading markers and "PRD:" prefix
            title = line.lstrip("#").strip()
            for prefix in ("PRD:", "PRD -", "Problem Statement"):
                if title.lower().startswith(prefix.lower()):
                    title = title[len(prefix) :].strip()
            return title[:100] if title else ""
    return ""
