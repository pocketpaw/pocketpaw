"""Mission Control Task Executor.

Created: 2026-02-05
Updated: 2026-02-26 — Deep Work v2: Added task retry, timeout, output storage,
  and project-wide stop. Tasks now store output on task.output field. Failed tasks
  auto-retry up to max_retries. Tasks with timeout_minutes get asyncio.wait_for.
  New: stop_all_project_tasks() for cancel/pause.
Updated: 2026-02-12 - Fixed execute_task_background self-defeating bug.

Enables execution of AI agents on tasks with real-time streaming via WebSocket.

Key features:
- Creates dedicated AgentRouter per task for isolation
- Uses agent's backend field (claude_agent_sdk, pocketpaw_native, open_interpreter)
- Streams execution to activity feed
- Updates task/agent status automatically
- Broadcasts events via MessageBus → WebSocket
- Auto-saves task output as deliverable document on completion
- Auto-retry failed tasks (configurable max_retries per task)
- Per-task timeout via asyncio.wait_for
- Stores output directly on Task.output for cross-task chaining

Security features:
- Max concurrent task limit (default: 5)
- UUID validation for task_id and agent_id
- Error message sanitization (no sensitive details exposed)
- Security audit logging

WebSocket Events:
- mc_task_started: Task execution begins
- mc_task_output: Agent produces output
- mc_task_completed: Execution ends (done/error/stopped/timeout)
- mc_task_retry: Task being retried after failure
- mc_activity_created: Activity logged
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

# UUID validation pattern
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Security constants
MAX_CONCURRENT_TASKS = 5  # Prevent resource exhaustion
MAX_ERROR_MESSAGE_LENGTH = 200  # Truncate error messages

from pocketpaw.agents.router import AgentRouter  # noqa: E402
from pocketpaw.bus.events import SystemEvent  # noqa: E402
from pocketpaw.bus.queue import get_message_bus  # noqa: E402
from pocketpaw.config import get_settings  # noqa: E402
from pocketpaw.mission_control.manager import get_mission_control_manager  # noqa: E402
from pocketpaw.mission_control.models import (  # noqa: E402
    Activity,
    ActivityType,
    AgentStatus,
    TaskStatus,
    now_iso,
)

logger = logging.getLogger(__name__)


class MCTaskExecutor:
    """Executes Mission Control tasks with AI agents.

    Creates isolated agent instances per task and broadcasts execution
    events via the MessageBus for real-time WebSocket updates.

    Usage:
        executor = get_mc_task_executor()
        await executor.execute_task(task_id, agent_id)
    """

    def __init__(self):
        """Initialize the executor."""
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._agent_routers: dict[str, AgentRouter] = {}
        self._stop_flags: dict[str, bool] = {}
        self._stream_errors: dict[str, str] = {}
        self._background_launched: set[str] = set()
        # Callback for direct scheduler integration (avoids MessageBus dependency
        # on the critical task-completion → cascade-dispatch path).
        # Set by DeepWorkSession.
        self._on_task_done_callback = None

    async def execute_task(
        self,
        task_id: str,
        agent_id: str,
    ) -> dict[str, Any]:
        """Execute a task with the specified agent.

        Creates a dedicated AgentRouter for the task, streams output
        via WebSocket, and updates task/agent status.

        Security:
        - Validates task_id and agent_id are valid UUIDs
        - Enforces max concurrent task limit
        - Sanitizes error messages before broadcast

        Args:
            task_id: ID of the task to execute
            agent_id: ID of the agent to run

        Returns:
            Dict with execution result:
            - status: "completed" | "error" | "stopped"
            - output: Full output from agent
            - error: Error message if failed
        """
        # Security: Validate input IDs are valid UUIDs
        if not self._is_valid_uuid(task_id):
            logger.warning(f"Security: Invalid task_id format: {task_id[:50]}")
            return {"status": "error", "error": "Invalid task ID format"}

        if not self._is_valid_uuid(agent_id):
            logger.warning(f"Security: Invalid agent_id format: {agent_id[:50]}")
            return {"status": "error", "error": "Invalid agent ID format"}

        # Security: Rate limit - check concurrent task count.
        # Note: execute_task_background also checks before registering to
        # prevent the race where all tasks register first, then all fail.
        if len(self._running_tasks) >= MAX_CONCURRENT_TASKS:
            logger.warning(
                f"Security: Max concurrent tasks ({MAX_CONCURRENT_TASKS}) reached. "
                f"Rejecting task {task_id}"
            )
            # Clean up leaked _running_tasks entry (if added by execute_task_background)
            self._running_tasks.pop(task_id, None)
            self._background_launched.discard(task_id)
            return {
                "status": "error",
                "error": f"Maximum concurrent tasks ({MAX_CONCURRENT_TASKS}) reached.",
            }

        manager = get_mission_control_manager()

        # Load task and agent
        task = await manager.get_task(task_id)
        if not task:
            return {"status": "error", "error": "Task not found"}

        agent = await manager.get_agent(agent_id)
        if not agent:
            return {"status": "error", "error": "Agent not found"}

        # Check if task is already running (skip if we launched it via background)
        if task_id in self._running_tasks and task_id not in self._background_launched:
            return {"status": "error", "error": "Task is already running"}
        self._background_launched.discard(task_id)

        # Security: Log task execution start
        logger.info(
            f"Task execution starting: task={task_id}, agent={agent_id}, "
            f"agent_name={agent.name}, task_title={task.title}"
        )

        # Initialize stop flag
        self._stop_flags[task_id] = False

        # Build agent settings with the agent's backend.
        # bypass_permissions is ALWAYS True for task execution because
        # tasks run headlessly (no terminal for interactive prompts).
        # The PreToolUse hook still blocks dangerous commands.
        base_settings = get_settings()
        agent_settings = base_settings.model_copy(
            update={"agent_backend": agent.backend, "bypass_permissions": True}
        )

        # Create dedicated router for this task
        router = AgentRouter(agent_settings)
        self._agent_routers[task_id] = router

        # Update task and agent status
        await manager.update_task_status(task_id, TaskStatus.IN_PROGRESS, agent_id)
        await manager.set_agent_status(agent_id, AgentStatus.ACTIVE, task_id)

        # Broadcast task started event
        await self._broadcast_event(
            "mc_task_started",
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "agent_name": agent.name,
                "task_title": task.title,
                "timestamp": now_iso(),
            },
        )

        # Log activity
        await self._log_activity(
            ActivityType.TASK_UPDATED,
            agent_id=agent_id,
            task_id=task_id,
            message=f"{agent.name} started working on '{task.title}'",
        )

        # Build the prompt for the agent
        prompt = await self._build_task_prompt(task, agent)

        # Execute and collect output
        output_chunks: list[str] = []
        final_status = "completed"
        error_message = None

        try:
            # Wrap execution with timeout if configured
            if task.timeout_minutes and task.timeout_minutes > 0:
                timeout_seconds = task.timeout_minutes * 60
                try:
                    await asyncio.wait_for(
                        self._stream_task(router, prompt, task_id, output_chunks),
                        timeout=timeout_seconds,
                    )
                except TimeoutError:
                    error_message = f"Task timed out after {task.timeout_minutes} minutes"
                    final_status = "timeout"
                    logger.warning(f"Task {task_id} timed out after {task.timeout_minutes}m")
            else:
                await self._stream_task(router, prompt, task_id, output_chunks)

            # Check if streaming set an error
            if task_id in self._stop_flags and self._stop_flags[task_id]:
                final_status = "stopped"
            elif final_status not in ("timeout",):
                # Check for error set during streaming via metadata
                stream_error = self._stream_errors.pop(task_id, None)
                if stream_error:
                    error_message = stream_error
                    final_status = "error"

        except Exception as e:
            logger.exception(f"Error executing task {task_id}")
            # Security: Sanitize error message - don't expose internal details
            error_message = self._sanitize_error(str(e))
            final_status = "error"

        finally:
            # Cleanup
            self._agent_routers.pop(task_id, None)
            self._running_tasks.pop(task_id, None)
            self._stop_flags.pop(task_id, None)
            self._stream_errors.pop(task_id, None)

            full_output = "".join(output_chunks)

            # Store output directly on task for cross-task chaining
            task_fresh = await manager.get_task(task_id)
            if task_fresh:
                if full_output:
                    task_fresh.output = full_output
                if error_message:
                    task_fresh.error_message = error_message

            # Determine task status and handle retry
            should_retry = False
            if final_status == "completed":
                new_task_status = TaskStatus.DONE
            elif final_status in ("error", "timeout") and task_fresh:
                # Check if we should retry
                if task_fresh.retry_count < task_fresh.max_retries:
                    should_retry = True
                    task_fresh.retry_count += 1
                    new_task_status = TaskStatus.ASSIGNED  # Reset for retry
                    logger.info(
                        f"Task {task_id} will retry "
                        f"({task_fresh.retry_count}/{task_fresh.max_retries}): "
                        f"{error_message}"
                    )
                else:
                    new_task_status = TaskStatus.BLOCKED
            else:
                new_task_status = TaskStatus.BLOCKED

            # Persist task updates (output, error, retry_count, status)
            if task_fresh:
                task_fresh.status = new_task_status
                task_fresh.updated_at = now_iso()
                if new_task_status == TaskStatus.DONE:
                    task_fresh.completed_at = now_iso()
                await manager.save_task(task_fresh)

            await manager.set_agent_status(agent_id, AgentStatus.IDLE, None)

            # Broadcast completion
            await self._broadcast_event(
                "mc_task_completed",
                {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "status": final_status,
                    "error": error_message,
                    "retry": should_retry,
                    "retry_count": task_fresh.retry_count if task_fresh else 0,
                    "max_retries": task_fresh.max_retries if task_fresh else 0,
                    "timestamp": now_iso(),
                },
            )

            # Log completion activity
            if final_status == "completed":
                await self._log_activity(
                    ActivityType.TASK_COMPLETED,
                    agent_id=agent_id,
                    task_id=task_id,
                    message=f"{agent.name} completed '{task.title}'",
                )

                # Save task output as a deliverable document
                if full_output:
                    await self._save_task_deliverable(
                        task_id=task_id,
                        agent_id=agent_id,
                        output=full_output,
                        task_title=task.title,
                    )

            elif should_retry:
                await self._log_activity(
                    ActivityType.TASK_UPDATED,
                    agent_id=agent_id,
                    task_id=task_id,
                    message=(
                        f"{agent.name} retrying '{task.title}' "
                        f"(attempt {task_fresh.retry_count}/{task_fresh.max_retries}): "
                        f"{error_message}"
                    ),
                )
                # Broadcast retry event for frontend
                await self._broadcast_event(
                    "mc_task_retry",
                    {
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "retry_count": task_fresh.retry_count if task_fresh else 0,
                        "max_retries": task_fresh.max_retries if task_fresh else 0,
                        "error": error_message,
                        "timestamp": now_iso(),
                    },
                )
                # Re-dispatch for retry
                asyncio.create_task(self.execute_task_background(task_id, agent_id))

            elif final_status in ("error", "timeout"):
                await self._log_activity(
                    ActivityType.TASK_UPDATED,
                    agent_id=agent_id,
                    task_id=task_id,
                    message=(
                        f"{agent.name} failed on '{task.title}' (no retries left): {error_message}"
                    ),
                )
            elif final_status == "stopped":
                await self._log_activity(
                    ActivityType.TASK_UPDATED,
                    agent_id=agent_id,
                    task_id=task_id,
                    message=f"Execution stopped for '{task.title}'",
                )

            # Direct scheduler callback — bypasses MessageBus for reliable
            # cascade dispatch (unblock dependents, check project completion).
            # Skip callback if we're retrying (task isn't actually done yet).
            if self._on_task_done_callback and not should_retry:
                try:
                    await self._on_task_done_callback(task_id)
                except Exception as e:
                    logger.warning(f"Scheduler callback failed for task {task_id}: {e}")

        return {
            "status": final_status,
            "output": full_output,
            "error": error_message,
        }

    async def _stream_task(
        self,
        router: AgentRouter,
        prompt: str,
        task_id: str,
        output_chunks: list[str],
    ) -> None:
        """Stream agent execution output, collecting chunks and broadcasting events.

        Separated from execute_task so it can be wrapped in asyncio.wait_for
        for timeout support.

        Args:
            router: The AgentRouter running this task.
            prompt: The assembled task prompt.
            task_id: ID of the task being executed.
            output_chunks: Mutable list to collect output chunks into.
        """
        async for chunk in router.run(prompt):
            # Check stop flag
            if self._stop_flags.get(task_id):
                break

            chunk_type = chunk.type
            content = chunk.content or ""
            meta = chunk.metadata or {}

            if chunk_type == "message" and content:
                output_chunks.append(content)
                await self._broadcast_event(
                    "mc_task_output",
                    {
                        "task_id": task_id,
                        "content": content,
                        "output_type": "message",
                        "timestamp": now_iso(),
                    },
                )

            elif chunk_type == "tool_use":
                tool_name = meta.get("name", "unknown")
                await self._broadcast_event(
                    "mc_task_output",
                    {
                        "task_id": task_id,
                        "content": f"Using tool: {tool_name}",
                        "output_type": "tool_use",
                        "timestamp": now_iso(),
                    },
                )

            elif chunk_type == "tool_result":
                result = content[:200] if content else ""
                await self._broadcast_event(
                    "mc_task_output",
                    {
                        "task_id": task_id,
                        "content": f"Tool result: {result}",
                        "output_type": "tool_result",
                        "timestamp": now_iso(),
                    },
                )

            elif chunk_type == "error":
                self._stream_errors[task_id] = content
                break

            elif chunk_type == "done":
                break

    async def execute_task_background(
        self,
        task_id: str,
        agent_id: str,
    ) -> bool:
        """Start task execution in the background.

        Returns immediately. Task runs in a background asyncio task.
        Use stop_task() to cancel execution.

        Guards against double-dispatch: if task_id is already tracked in
        _running_tasks the call is silently skipped.  A cleanup wrapper
        ensures the tracking entry is removed even when execute_task
        returns early (e.g. validation failure), preventing zombie entries.

        Args:
            task_id: ID of the task to execute
            agent_id: ID of the agent to run

        Returns:
            True if task was launched, False if rejected (capacity full).
        """
        # Check capacity BEFORE registering to prevent the race condition
        # where N tasks all register, then all N see len >= limit and reject.
        if len(self._running_tasks) >= MAX_CONCURRENT_TASKS:
            logger.info(
                f"Deferring task {task_id}: at capacity "
                f"({len(self._running_tasks)}/{MAX_CONCURRENT_TASKS})"
            )
            return False

        # Guard against double-dispatch
        if task_id in self._running_tasks:
            logger.warning(f"Task {task_id} is already running, skipping duplicate dispatch")
            return False

        # Mark as pending so execute_task knows it was launched via background
        # (avoids race where execute_task sees task_id in _running_tasks
        # because we registered it before the coroutine started)
        self._background_launched.add(task_id)
        async_task = asyncio.create_task(self.execute_task(task_id, agent_id))
        self._running_tasks[task_id] = async_task
        return True

    async def stop_task(self, task_id: str) -> bool:
        """Stop a running task.

        Args:
            task_id: ID of the task to stop

        Returns:
            True if task was stopped, False if not running
        """
        if task_id not in self._running_tasks:
            return False

        # Set stop flag
        self._stop_flags[task_id] = True

        # Stop the agent router if exists
        router = self._agent_routers.get(task_id)
        if router:
            try:
                await router.stop()
            except Exception as e:
                logger.warning(f"Error stopping router for task {task_id}: {e}")

        # Cancel the asyncio task
        async_task = self._running_tasks.get(task_id)
        if async_task and not async_task.done():
            async_task.cancel()
            try:
                await async_task
            except asyncio.CancelledError:
                pass

        logger.info(f"Stopped task execution: {task_id}")
        return True

    async def stop_all_project_tasks(self, project_id: str) -> int:
        """Stop all running tasks belonging to a project.

        Used by project cancellation and pause.

        Args:
            project_id: ID of the project whose tasks to stop.

        Returns:
            Number of tasks stopped.
        """
        manager = get_mission_control_manager()
        tasks = await manager.get_project_tasks(project_id)
        stopped = 0
        for task in tasks:
            if self.is_task_running(task.id):
                await self.stop_task(task.id)
                stopped += 1
        return stopped

    def is_task_running(self, task_id: str) -> bool:
        """Check if a task is currently running.

        Args:
            task_id: ID of the task to check

        Returns:
            True if task is running
        """
        return task_id in self._running_tasks

    def get_running_tasks(self) -> list[str]:
        """Get list of currently running task IDs.

        Returns:
            List of task IDs
        """
        return list(self._running_tasks.keys())

    def _is_valid_uuid(self, value: str) -> bool:
        """Validate that a string is a valid UUID.

        Security: Prevents injection via malformed IDs.

        Args:
            value: String to validate

        Returns:
            True if valid UUID format
        """
        if not value or not isinstance(value, str):
            return False
        return bool(UUID_PATTERN.match(value))

    def _sanitize_error(self, error: str) -> str:
        """Sanitize error message for safe broadcast.

        Security: Removes potentially sensitive information like:
        - File paths
        - API keys
        - Stack traces
        - Internal implementation details

        Args:
            error: Raw error message

        Returns:
            Sanitized error message
        """
        if not error:
            return "An error occurred"

        # Truncate to max length
        sanitized = error[:MAX_ERROR_MESSAGE_LENGTH]

        # Remove potential file paths
        sanitized = re.sub(r"/[^\s]+/[^\s]+", "[path]", sanitized)

        # Remove potential API keys or tokens
        sanitized = re.sub(
            r"(key|token|secret|password)[=:]\s*\S+",
            r"\1=[redacted]",
            sanitized,
            flags=re.IGNORECASE,
        )

        # If truncated, add indicator
        if len(error) > MAX_ERROR_MESSAGE_LENGTH:
            sanitized = sanitized.rstrip() + "..."

        return sanitized

    async def _build_task_prompt(self, task, agent) -> str:
        """Build the prompt to send to the agent.

        Includes agent identity, task details, project context (PRD summary,
        upstream deliverables), and project working directory.
        """
        manager = get_mission_control_manager()

        prompt_parts = [
            f"You are {agent.name}, a {agent.role}.",
        ]

        if agent.description:
            prompt_parts.append(f"Description: {agent.description}")

        if agent.specialties:
            prompt_parts.append(f"Specialties: {', '.join(agent.specialties)}")

        # Project context: PRD and working directory
        if task.project_id:
            project = await manager.get_project(task.project_id)
            if project:
                from pocketpaw.mission_control.manager import get_project_dir

                project_dir = get_project_dir(project.id)
                prompt_parts.extend(
                    [
                        "",
                        "## Project Context",
                        f"**Project:** {project.title}",
                        f"**Working Directory:** {project_dir}",
                    ]
                )

                # Include PRD summary (first 2000 chars)
                if project.prd_document_id:
                    prd_doc = await manager.get_document(project.prd_document_id)
                    if prd_doc and prd_doc.content:
                        prd_summary = prd_doc.content[:2000]
                        if len(prd_doc.content) > 2000:
                            prd_summary += "\n... (truncated)"
                        prompt_parts.extend(
                            [
                                "",
                                "### Requirements (PRD)",
                                prd_summary,
                            ]
                        )

            # Include deliverables from upstream (completed dependency) tasks
            if task.blocked_by:
                upstream_outputs = []
                for dep_id in task.blocked_by:
                    dep_task = await manager.get_task(dep_id)
                    if dep_task and dep_task.status in (TaskStatus.DONE,):
                        # Find deliverable document for this task
                        docs = await manager.get_task_documents(dep_id)
                        for doc in docs:
                            if doc.content:
                                snippet = doc.content[:1000]
                                if len(doc.content) > 1000:
                                    snippet += "\n... (truncated)"
                                upstream_outputs.append(f"**{dep_task.title}:**\n{snippet}")

                if upstream_outputs:
                    prompt_parts.extend(
                        [
                            "",
                            "### Upstream Task Outputs",
                            "The following tasks have been completed before yours. "
                            "Use their output as context:",
                            "",
                        ]
                    )
                    prompt_parts.extend(upstream_outputs)

        prompt_parts.extend(
            [
                "",
                "## Task",
                f"**Title:** {task.title}",
            ]
        )

        if task.description:
            prompt_parts.append(f"**Description:** {task.description}")

        prompt_parts.extend(
            [
                f"**Priority:** {task.priority.value}",
                "",
                "Please complete this task. Provide your work and findings.",
            ]
        )

        return "\n".join(prompt_parts)

    async def _broadcast_event(
        self,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Broadcast an event via the MessageBus.

        Events are picked up by the WebSocket adapter and sent to clients.

        Args:
            event_type: Type of event (mc_task_started, mc_task_output, etc.)
            data: Event data
        """
        bus = get_message_bus()
        event = SystemEvent(
            event_type=event_type,
            data=data,
            timestamp=datetime.now(UTC),
        )
        await bus.publish_system(event)

    async def _log_activity(
        self,
        activity_type: ActivityType,
        agent_id: str | None = None,
        task_id: str | None = None,
        message: str = "",
    ) -> Activity:
        """Log an activity and broadcast it via WebSocket.

        Args:
            activity_type: Type of activity
            agent_id: Agent that triggered the activity
            task_id: Related task
            message: Human-readable description

        Returns:
            The created Activity
        """
        manager = get_mission_control_manager()

        activity = Activity(
            type=activity_type,
            agent_id=agent_id,
            task_id=task_id,
            message=message,
        )
        await manager.save_activity(activity)

        # Broadcast activity created event
        await self._broadcast_event(
            "mc_activity_created",
            {
                "activity": activity.to_dict(),
            },
        )

        return activity

    async def _save_task_deliverable(
        self,
        task_id: str,
        agent_id: str,
        output: str,
        task_title: str,
    ) -> None:
        """Save agent output as a deliverable document.

        Creates a Document of type DELIVERABLE linked to the task.
        This persists the agent's work for later review.

        Args:
            task_id: ID of the completed task
            agent_id: ID of the agent that completed the task
            output: Full text output from the agent
            task_title: Title of the task (for document title)
        """
        from pocketpaw.mission_control.models import Document, DocumentType

        if not output or not output.strip():
            return

        manager = get_mission_control_manager()

        # Create deliverable document
        document = Document(
            title=f"Deliverable: {task_title}",
            content=output,
            type=DocumentType.DELIVERABLE,
            author_id=agent_id,
            task_id=task_id,
            tags=["auto-generated", "task-output"],
        )

        await manager.save_document(document)

        logger.info(
            f"Saved task deliverable: doc_id={document.id}, task_id={task_id}, length={len(output)}"
        )

        # Log activity
        await self._log_activity(
            ActivityType.DOCUMENT_CREATED,
            agent_id=agent_id,
            task_id=task_id,
            message=f"Deliverable saved for '{task_title}'",
        )


# Singleton pattern
_executor_instance: MCTaskExecutor | None = None


def get_mc_task_executor() -> MCTaskExecutor:
    """Get or create the MC Task Executor singleton.

    Returns:
        The MCTaskExecutor instance
    """
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = MCTaskExecutor()
    return _executor_instance


def reset_mc_task_executor() -> None:
    """Reset the executor singleton (for testing)."""
    global _executor_instance
    _executor_instance = None
