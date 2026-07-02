"""Mission Control Task Executor.

Created: 2026-02-05
Updated: 2026-07-02 — LLM-as-judge SHADOW stamp (J-1, #1168): inside the
  existing ``deep_work_verify_loop_enabled`` block, AFTER the deterministic
  verdict is computed and stamped, when ``deep_work_verify_judge_shadow_enabled``
  is ALSO on the LlmJudgeVerdictProvider scores the same (output,
  success_criteria) and its verdict is stamped observe-only on
  ``task.metadata["verify_judge_verdict"]`` plus ONE structured log line
  comparing the two verdicts at status level (``verify judge shadow:
  deterministic=<s> judge=<s> agree=<bool> task=<id>``). The judge verdict
  NEVER feeds the requeue/escalate decision in any branch — the deterministic
  verdict alone drives behaviour, exactly as today. Judge exceptions are
  swallowed to a debug log (shadow must never break completion). Judge flag
  off ⇒ the provider is never constructed (no subprocess); loop flag off ⇒
  the whole block is skipped, byte-for-byte today's behaviour.
Updated: 2026-06-23 — Self-Verifying Loop (SVL-3): a no-progress / oscillation
  guard now runs INSIDE the PARTIAL / NOT_SOLVED branch, BEFORE the SVL-2
  budget-bounded requeue decision. When there is a prior attempt to compare
  against, it builds the current unmet-criteria fingerprint
  (``frozenset(cr.criterion for cr in unmet)``) and the previous attempt's
  fingerprint from the last ``verify_feedback`` record. If the task is making
  no progress — the SAME criteria still failing (``current == prev``,
  oscillation/stuck) OR the unmet count not shrinking (``len(current) >=
  len(prev)``, divergence) — it escalates to BLOCKED EARLY with
  ``verify_escalation_reason="no_progress"`` instead of burning the whole
  requeue budget circling a dead end (``should_retry`` stays False so the
  existing escalation cascade handles it). A task that IS progressing (strictly
  shrinking, different unmet set) falls through to the unchanged SVL-2 requeue
  path. The escalation cascade branch now reads ``verify_escalation_reason`` off
  the task and tailors the operator-facing activity/broadcast to the actual
  cause (budget_exhausted vs no_progress) rather than assuming budget burn. Flag
  off ⇒ behaviour is byte-for-byte unchanged.
Updated: 2026-06-23 — Self-Verifying Loop (SVL-2): the completion hook now
  ACTS on the SVL-1 verdict. A SOLVED / UNKNOWN result still passes through as
  DONE (a passing or uncheckable result is never requeued or mutated). A
  PARTIAL / NOT_SOLVED result is requeued: the SPECIFIC unmet criteria (+ their
  details) are appended to a cumulative ``verify_feedback`` log, the task is set
  back to ASSIGNED and re-dispatched via the EXISTING error-retry mechanism
  (``should_retry`` lever + ``execute_task_background``), bounded by
  ``deep_work_verify_max_requeues`` via a SEPARATE ``verify_requeue_count``
  counter (independent of the error ``retry_count``). When the budget is
  exhausted the task escalates to BLOCKED with
  ``verify_escalation_reason="budget_exhausted"``. ``_build_task_prompt`` now
  renders a "## Why the last attempt was rejected" section from
  ``verify_feedback`` so the re-dispatched agent sees exactly what to fix. The
  DONE side-effects (TASK_COMPLETED activity + deliverable save) are gated on
  the RESOLVED ``new_task_status == DONE``, so neither a verify-requeue
  (ASSIGNED) nor a verify-escalation (BLOCKED) marks failing work as completed
  or saves its rejected output; an escalation logs a needs-review activity and
  broadcasts ``mc_task_blocked`` instead. Flag off ⇒ behaviour is byte-for-byte
  unchanged.
Updated: 2026-06-23 — Self-Verifying Loop (SVL-1): when
  ``deep_work_verify_loop_enabled`` is set, a successfully-completing task has
  its output checked against the ``success_criteria`` in its metadata via a
  DeterministicVerdictProvider, and the resulting OutcomeVerdict is stamped on
  ``task.metadata["verify_verdict"]`` and logged. Observe-only — the task's
  status is unchanged (no requeue here; that is SVL-2). Flag off ⇒ behaviour
  is byte-for-byte unchanged.
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
from pocketpaw.instinct.judge_provider import LlmJudgeVerdictProvider  # noqa: E402
from pocketpaw.instinct.models import OutcomeStatus  # noqa: E402
from pocketpaw.instinct.verdict_provider import DeterministicVerdictProvider  # noqa: E402
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

                # Self-Verifying Loop: when the loop is enabled, check the
                # completed task's result against the success_criteria captured
                # at intake and STAMP the verdict on the task — the "verify"
                # half (SVL-1). SVL-2 then ACTS on the verdict: a result that
                # does NOT meet its criteria is requeued with the specific unmet
                # criteria fed back, up to a budget, then escalated to BLOCKED.
                # When the flag is off this whole block is skipped and behaviour
                # is byte-for-byte unchanged.
                if get_settings().deep_work_verify_loop_enabled and task_fresh:
                    success_criteria = task_fresh.metadata.get("success_criteria", [])
                    verdict = DeterministicVerdictProvider().verify(
                        task_fresh.output, success_criteria
                    )
                    task_fresh.metadata["verify_verdict"] = verdict.model_dump()
                    logger.info(
                        "Task %s verify verdict: %s (%s)",
                        task_id,
                        verdict.status,
                        verdict.summary,
                    )

                    # J-1 (shadow): when the judge flag is ALSO on, score the
                    # SAME (output, criteria) with the LLM-as-judge provider
                    # and stamp its verdict ALONGSIDE the deterministic one —
                    # observe-only. The judge verdict NEVER feeds the
                    # requeue/escalate decision below; the deterministic
                    # verdict alone drives behaviour, exactly as today. Any
                    # judge failure is swallowed to a debug log — shadow must
                    # never break task completion.
                    if get_settings().deep_work_verify_judge_shadow_enabled:
                        try:
                            judge_verdict = await LlmJudgeVerdictProvider().verify(
                                task_fresh.output, success_criteria
                            )
                            task_fresh.metadata["verify_judge_verdict"] = judge_verdict.model_dump()
                            logger.info(
                                "verify judge shadow: deterministic=%s judge=%s agree=%s task=%s",
                                verdict.status.value,
                                judge_verdict.status.value,
                                verdict.status == judge_verdict.status,
                                task_id,
                            )
                        except Exception:
                            logger.debug(
                                "verify judge shadow failed for task %s — "
                                "ignored (shadow never breaks completion)",
                                task_id,
                                exc_info=True,
                            )

                    # SVL-2: act on the verdict. SOLVED / UNKNOWN pass through
                    # as DONE — a passing (or uncheckable) result is NEVER
                    # requeued or mutated. PARTIAL / NOT_SOLVED means the output
                    # missed at least one captured criterion: requeue the task
                    # with the unmet criteria fed back, bounded by
                    # deep_work_verify_max_requeues, then escalate to BLOCKED.
                    if verdict.status in (
                        OutcomeStatus.PARTIAL,
                        OutcomeStatus.NOT_SOLVED,
                    ):
                        unmet = [cr for cr in verdict.criteria_results if not cr.met]
                        # Verify-specific counter, SEPARATE from the error
                        # retry_count so verify requeues and error retries never
                        # share a budget.
                        n = task_fresh.metadata.get("verify_requeue_count", 0)
                        max_requeues = get_settings().deep_work_verify_max_requeues

                        # SVL-3 no-progress / oscillation guard: a task can fail
                        # the SAME criteria attempt after attempt and burn the
                        # whole requeue budget circling a dead end. Before the
                        # budget-bounded requeue decision, compare THIS attempt's
                        # unmet set against the PREVIOUS attempt's. If it is
                        # making no progress — the exact same criteria still
                        # failing (oscillation/stuck), OR the unmet count not
                        # shrinking (divergence) — escalate to BLOCKED EARLY
                        # rather than wait for the budget. A task that is making
                        # progress (strictly shrinking, different set) falls
                        # through to the existing SVL-2 requeue logic unchanged.
                        prior_feedback = task_fresh.metadata.get("verify_feedback")
                        no_progress = False
                        if prior_feedback:
                            current = frozenset(cr.criterion for cr in unmet)
                            prev = frozenset(
                                item["criterion"]
                                for item in prior_feedback[-1].get("unmet_criteria", [])
                            )
                            no_progress = current == prev or len(current) >= len(prev)

                        if no_progress:
                            # Same/non-shrinking unmet set: stuck. Escalate to
                            # the SAME BLOCKED status + cascade branch as a
                            # budget exhaustion, distinguished only by the
                            # reason. should_retry stays False so the escalation
                            # cascade — not the requeue path — handles it.
                            new_task_status = TaskStatus.BLOCKED
                            task_fresh.metadata["verify_escalation_reason"] = "no_progress"
                            logger.info(
                                "Task %s made no progress across requeues (unmet set "
                                "unchanged/not shrinking); escalating to BLOCKED",
                                task_id,
                            )
                        elif n < max_requeues:
                            # Append this attempt's unmet criteria to the
                            # CUMULATIVE feedback log so attempt N sees the
                            # rejections from attempts 1..N-1.
                            task_fresh.metadata.setdefault("verify_feedback", []).append(
                                {
                                    "attempt": n + 1,
                                    "status": verdict.status.value,
                                    "summary": verdict.summary,
                                    "unmet_criteria": [
                                        {
                                            "criterion": cr.criterion,
                                            "detail": cr.detail,
                                        }
                                        for cr in unmet
                                    ],
                                }
                            )
                            task_fresh.metadata["verify_requeue_count"] = n + 1
                            # Mirror the error-retry branch: ASSIGNED + the
                            # should_retry lever drive the re-dispatch and the
                            # callback-skip; no new dispatch machinery.
                            new_task_status = TaskStatus.ASSIGNED
                            should_retry = True
                            logger.info(
                                "Task %s failed verify (%s); requeue %d/%d with "
                                "%d unmet criterion(s)",
                                task_id,
                                verdict.status,
                                n + 1,
                                max_requeues,
                                len(unmet),
                            )
                        else:
                            # Budget exhausted — escalate to BLOCKED (the status
                            # the manual-retry endpoint already admits) and stamp
                            # the reason so the operator sees why it stuck.
                            new_task_status = TaskStatus.BLOCKED
                            task_fresh.metadata["verify_escalation_reason"] = "budget_exhausted"
                            logger.info(
                                "Task %s failed verify (%s) after %d requeue(s); "
                                "escalating to BLOCKED (budget exhausted)",
                                task_id,
                                verdict.status,
                                n,
                            )
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

            # Log completion activity. The DONE side-effects (completion log +
            # deliverable save) must run ONLY when the task actually landed
            # DONE. SVL-2 keeps final_status == "completed" while diverting a
            # failing result to either ASSIGNED (verify-requeue) or BLOCKED
            # (verify-escalation), so this branch is gated on the resolved
            # ``new_task_status`` — not on ``final_status`` alone. SOLVED /
            # UNKNOWN / flag-off all resolve to DONE and enter here unchanged; a
            # requeued or escalated failing result never logs "completed" or
            # saves its rejected output as a deliverable.
            if final_status == "completed" and new_task_status == TaskStatus.DONE:
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

            elif final_status == "completed" and new_task_status == TaskStatus.BLOCKED:
                # Verify-escalation: the result never met its criteria. Surface
                # it as a needs-review escalation in the activity feed — NOT a
                # completion — and broadcast so an operator sees it. The
                # escalation has two causes (SVL-2 budget exhaustion, SVL-3
                # no-progress); read the actual reason off the task so the
                # operator sees WHY it stuck rather than assuming budget burn.
                verify_n = task_fresh.metadata.get("verify_requeue_count", 0) if task_fresh else 0
                verify_max = get_settings().deep_work_verify_max_requeues
                escalation_reason = (
                    task_fresh.metadata.get("verify_escalation_reason") if task_fresh else None
                )
                if escalation_reason == "no_progress":
                    cause = "output kept failing the same success criteria with no progress"
                else:
                    cause = f"output failed its success criteria after {verify_max} requeue(s)"
                await self._log_activity(
                    ActivityType.TASK_UPDATED,
                    agent_id=agent_id,
                    task_id=task_id,
                    message=(f"{agent.name} escalated '{task.title}' — {cause}; needs review"),
                )
                await self._broadcast_event(
                    "mc_task_blocked",
                    {
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "reason": "verify_escalation",
                        "escalation_reason": escalation_reason,
                        "verify_requeue_count": verify_n,
                        "verify_max_requeues": verify_max,
                        "timestamp": now_iso(),
                    },
                )

            elif should_retry:
                # should_retry is set by two paths: the error/timeout auto-retry
                # branch (carries error_message + the error retry_count) and the
                # SVL-2 verify requeue (no error_message; tracked by the separate
                # verify_requeue_count). Pick the message + counters per path so
                # an operator sees the right story.
                is_verify_requeue = final_status == "completed"
                if is_verify_requeue:
                    verify_n = (
                        task_fresh.metadata.get("verify_requeue_count", 0) if task_fresh else 0
                    )
                    verify_max = get_settings().deep_work_verify_max_requeues
                    await self._log_activity(
                        ActivityType.TASK_UPDATED,
                        agent_id=agent_id,
                        task_id=task_id,
                        message=(
                            f"{agent.name} requeuing '{task.title}' — result did not "
                            f"meet its success criteria "
                            f"(verify requeue {verify_n}/{verify_max})"
                        ),
                    )
                    await self._broadcast_event(
                        "mc_task_retry",
                        {
                            "task_id": task_id,
                            "agent_id": agent_id,
                            "reason": "verify_requeue",
                            "verify_requeue_count": verify_n,
                            "verify_max_requeues": verify_max,
                            "timestamp": now_iso(),
                        },
                    )
                else:
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
                # Re-dispatch for retry / requeue — same mechanism for both.
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

        # Inject simulation tick context when running in tick-synchronized mode
        sim_tick = task.metadata.get("simulation_tick")
        if sim_tick is not None:
            prompt_parts.append(f"**Simulation Tick:** {sim_tick}")

        # Self-Verifying Loop (SVL-2): if a prior attempt was rejected because
        # its output missed one or more success criteria, surface the SPECIFIC
        # unmet criteria so the re-dispatched agent sees exactly what to fix
        # rather than a bare "try again". The feedback log is cumulative across
        # attempts (attempt N carries 1..N-1), so render every prior attempt's
        # rejections — most recent first.
        verify_feedback = task.metadata.get("verify_feedback")
        if verify_feedback:
            prompt_parts.extend(
                [
                    "",
                    "## Why the last attempt was rejected",
                    "A previous attempt did NOT meet the success criteria below. "
                    "Address each unmet criterion in your work this time:",
                ]
            )
            for record in reversed(verify_feedback):
                attempt = record.get("attempt")
                summary = record.get("summary", "")
                header = f"**Attempt {attempt}** ({summary}):" if attempt else f"**{summary}**:"
                prompt_parts.append(header)
                for item in record.get("unmet_criteria", []):
                    criterion = item.get("criterion", "")
                    detail = item.get("detail", "")
                    line = f"- {criterion}"
                    if detail:
                        line += f" — {detail}"
                    prompt_parts.append(line)

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
