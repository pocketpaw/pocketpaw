# Deep Work Dependency Scheduler
# Created: 2026-02-12
# Updated: 2026-02-12 — Treat SKIPPED status same as DONE for blocker resolution
#   and project completion checks.
#   Optimized: use get_project_tasks (no 100-limit), concurrent dispatch.
# Updated: 2026-03-26 — Added optional SimulationClock integration for tick-synchronized
#   dispatch mode (issue #633).
#
# Key features:
# - get_ready_tasks: finds tasks with all blockers satisfied (DONE or SKIPPED)
# - on_task_completed: auto-dispatches newly unblocked tasks
# - validate_graph: cycle detection via Kahn's algorithm (works with Task and TaskSpec)
# - get_execution_order: groups tasks by dependency level (works with Task and TaskSpec)
# - run_tick_synchronized: dispatch all ready tasks per tick, wait, then advance clock

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import TYPE_CHECKING

from pocketpaw.mission_control.models import Task, TaskStatus, now_iso

if TYPE_CHECKING:
    from pocketpaw.deep_work.clock import SimulationClock, TickSnapshot

logger = logging.getLogger(__name__)


def _get_id(item) -> str:
    """Extract the identifier from a Task (.id) or TaskSpec (.key)."""
    if hasattr(item, "key") and item.key:
        return item.key
    return item.id


def _get_deps(item) -> list[str]:
    """Extract dependency list from a Task (.blocked_by) or TaskSpec (.blocked_by_keys)."""
    if hasattr(item, "blocked_by_keys"):
        return item.blocked_by_keys
    return item.blocked_by


class DependencyScheduler:
    """Schedules and dispatches tasks based on dependency order.

    The scheduler does NOT subscribe to bus events itself. The session layer
    is responsible for wiring on_task_completed to the appropriate bus events.
    """

    def __init__(self, manager, executor, human_router=None, clock: SimulationClock | None = None):
        """Initialize the scheduler.

        Args:
            manager: MissionControlManager instance (list_tasks, get_task, etc.)
            executor: MCTaskExecutor instance (execute_task_background)
            human_router: Optional human notification router (notify_human_task, notify_review_task)
            clock: Optional SimulationClock for tick-synchronized dispatch mode.
        """
        self.manager = manager
        self.executor = executor
        self.human_router = human_router
        self.clock = clock

    async def get_ready_tasks(self, project_id: str) -> list[Task]:
        """Return tasks in project where all blockers are satisfied.

        A task is "ready" when:
        - Its status is INBOX or ASSIGNED (not yet started)
        - All task IDs in its blocked_by list have status DONE or SKIPPED

        Args:
            project_id: Project to check

        Returns:
            List of tasks ready to be dispatched
        """
        project_tasks = await self.manager.get_project_tasks(project_id)
        resolved_ids = {
            t.id for t in project_tasks if t.status in (TaskStatus.DONE, TaskStatus.SKIPPED)
        }

        ready = []
        for task in project_tasks:
            if task.status not in (TaskStatus.INBOX, TaskStatus.ASSIGNED):
                continue
            if not task.blocked_by or all(bid in resolved_ids for bid in task.blocked_by):
                ready.append(task)
        return ready

    async def on_task_completed(self, task_id: str):
        """Called when a task finishes. Dispatch newly unblocked tasks.

        Args:
            task_id: ID of the task that just completed
        """
        task = await self.manager.get_task(task_id)
        if not task or not task.project_id:
            return

        ready = await self.get_ready_tasks(task.project_id)
        if ready:
            # Dispatch all ready tasks concurrently
            await asyncio.gather(*(self._dispatch_task(t) for t in ready))

        # Check if entire project is now complete
        await self.check_project_completion(task.project_id)

    async def _dispatch_task(self, task: Task):
        """Dispatch a single task based on its type.

        Includes a status guard to prevent duplicate dispatch when multiple
        upstream tasks complete simultaneously.

        - agent tasks: sent to executor with first assignee
        - human tasks: routed to human_router.notify_human_task
        - review tasks: routed to human_router.notify_review_task

        Args:
            task: Task to dispatch
        """
        # Guard: re-fetch task to check it hasn't already been dispatched
        # by a concurrent on_task_completed call.
        fresh = await self.manager.get_task(task.id)
        if not fresh or fresh.status not in (TaskStatus.INBOX, TaskStatus.ASSIGNED):
            logger.debug(
                "Skipping dispatch for %s: status=%s",
                task.id,
                fresh.status if fresh else None,
            )
            return

        if fresh.task_type == "agent":
            agent_id = fresh.assignee_ids[0] if fresh.assignee_ids else None
            if agent_id:
                logger.info(f"Auto-dispatching agent task: {fresh.title}")
                await self.executor.execute_task_background(fresh.id, agent_id)
            else:
                logger.warning(f"Agent task has no assignee: {fresh.title}")
        elif fresh.task_type == "human":
            if self.human_router:
                logger.info(f"Routing human task: {fresh.title}")
                await self.human_router.notify_human_task(fresh)
            else:
                logger.warning(f"Human task but no router: {fresh.title}")
        elif fresh.task_type == "review":
            if self.human_router:
                logger.info(f"Routing review task: {fresh.title}")
                await self.human_router.notify_review_task(fresh)

    async def check_project_completion(self, project_id: str) -> bool:
        """Check if ALL tasks in project are DONE. If yes, mark project COMPLETED.

        Args:
            project_id: Project to check

        Returns:
            True if project is now completed
        """
        project_tasks = await self.manager.get_project_tasks(project_id)

        if not project_tasks:
            return False

        all_done = all(t.status in (TaskStatus.DONE, TaskStatus.SKIPPED) for t in project_tasks)
        if all_done:
            project = await self.manager.get_project(project_id)
            if project:
                from pocketpaw.deep_work.models import ProjectStatus

                project.status = ProjectStatus.COMPLETED
                project.completed_at = now_iso()
                await self.manager.update_project(project)
                logger.info(f"Project completed: {project.title}")
            return True
        return False

    @staticmethod
    def validate_graph(tasks: list) -> tuple[bool, str]:
        """Validate dependency graph for cycles and missing references.

        Uses Kahn's algorithm for topological sort. If any nodes remain
        after processing, the graph contains a cycle.

        Works with both TaskSpec (.key/.blocked_by_keys) and Task (.id/.blocked_by).

        Args:
            tasks: List of Task or TaskSpec objects

        Returns:
            (is_valid, error_message) tuple. error_message is empty if valid.
        """
        if not tasks:
            return True, ""

        # Build lookup of all known IDs
        all_ids = {_get_id(t) for t in tasks}

        # Check for references to non-existent tasks
        for task in tasks:
            for dep in _get_deps(task):
                if dep not in all_ids:
                    return False, f"Task '{_get_id(task)}' depends on non-existent task '{dep}'"

        # Kahn's algorithm: compute in-degree and adjacency
        in_degree: dict[str, int] = {_get_id(t): 0 for t in tasks}
        # adjacency: dep -> list of tasks that depend on it
        adjacency: dict[str, list[str]] = {_get_id(t): [] for t in tasks}

        for task in tasks:
            tid = _get_id(task)
            deps = _get_deps(task)
            in_degree[tid] = len(deps)
            for dep in deps:
                adjacency[dep].append(tid)

        # Start with zero in-degree nodes
        queue = deque(tid for tid, deg in in_degree.items() if deg == 0)
        processed = 0

        while queue:
            node = queue.popleft()
            processed += 1
            for dependent in adjacency[node]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if processed < len(tasks):
            # Find nodes still in the cycle for error message
            cycle_nodes = [tid for tid, deg in in_degree.items() if deg > 0]
            return False, f"Dependency cycle detected involving: {', '.join(sorted(cycle_nodes))}"

        return True, ""

    @staticmethod
    def get_execution_order(tasks: list) -> list[list[str]]:
        """Group tasks by dependency level for parallel execution.

        Level 0: tasks with no dependencies
        Level 1: tasks depending only on level 0 tasks
        Level N: tasks depending on level N-1 or lower

        Works with both TaskSpec (.key/.blocked_by_keys) and Task (.id/.blocked_by).

        Args:
            tasks: List of Task or TaskSpec objects

        Returns:
            List of lists, each inner list is a set of task IDs/keys
            that can execute in parallel at that level.
        """
        if not tasks:
            return []

        # Build maps
        task_map = {_get_id(t): t for t in tasks}
        all_ids = set(task_map.keys())

        # Compute levels via BFS (Kahn's algorithm variant)
        in_degree: dict[str, int] = {}
        adjacency: dict[str, list[str]] = {tid: [] for tid in all_ids}

        for task in tasks:
            tid = _get_id(task)
            deps = [d for d in _get_deps(task) if d in all_ids]
            in_degree[tid] = len(deps)
            for dep in deps:
                adjacency[dep].append(tid)

        # Current level starts with zero in-degree
        current_level = [tid for tid, deg in in_degree.items() if deg == 0]
        levels: list[list[str]] = []

        while current_level:
            levels.append(sorted(current_level))
            next_level = []
            for node in current_level:
                for dependent in adjacency[node]:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        next_level.append(dependent)
            current_level = next_level

        return levels

    # ------------------------------------------------------------------
    # Tick-synchronized dispatch (SimulationClock integration, issue #633)
    # ------------------------------------------------------------------

    async def run_tick_synchronized(self, project_id: str) -> list[TickSnapshot]:
        """Execute a project in tick-synchronized mode.

        Each tick:
        1. Gather all ready (unblocked) tasks.
        2. Inject ``current_tick`` into each task's metadata.
        3. Dispatch them concurrently and wait for all to complete.
        4. Record a :class:`TickSnapshot` of task states.
        5. Advance the clock.

        Repeats until no more tasks are ready (project complete or blocked).

        Requires ``self.clock`` to be set.

        Args:
            project_id: The project to execute.

        Returns:
            List of :class:`TickSnapshot` objects recorded during execution.

        Raises:
            RuntimeError: If no SimulationClock is attached.
        """
        if self.clock is None:
            raise RuntimeError(
                "run_tick_synchronized requires a SimulationClock — "
                "pass clock= to DependencyScheduler.__init__"
            )

        from pocketpaw.deep_work.clock import TickSnapshot

        while True:
            ready = await self.get_ready_tasks(project_id)
            if not ready:
                break

            # Stamp each task with the current tick
            for task in ready:
                task.metadata["simulation_tick"] = self.clock.current_tick
                await self.manager.save_task(task)

            # Dispatch all ready tasks and wait for completion
            await asyncio.gather(*(self._dispatch_task(t) for t in ready))

            # Record snapshot
            project_tasks = await self.manager.get_project_tasks(project_id)
            snapshot = TickSnapshot(
                tick=self.clock.current_tick,
                task_states={t.id: t.status.value for t in project_tasks},
            )
            self.clock.record_snapshot(snapshot)

            # Advance clock
            await self.clock.advance()

            # Check project completion
            done = await self.check_project_completion(project_id)
            if done:
                break

        return self.clock.get_snapshots()
