"""Mission Control Heartbeat System.

Created: 2026-02-05
Background daemon that wakes agents periodically to check for work.

The heartbeat system:
- Wakes each agent on a staggered schedule
- Checks for @mentions, assigned tasks, activity updates
- Records agent heartbeat timestamps
- Reports agent status (idle, active, blocked)

Based on PocketPaw's proactive daemon pattern.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from pocketpaw.mission_control.manager import get_mission_control_manager
from pocketpaw.mission_control.models import AgentStatus

logger = logging.getLogger(__name__)

# Default heartbeat interval in minutes
DEFAULT_HEARTBEAT_INTERVAL = 15


class HeartbeatDaemon:
    """Background daemon for agent heartbeats.

    Wakes agents periodically to check for work and update their status.
    Uses APScheduler for interval-based triggering.
    """

    def __init__(
        self,
        interval_minutes: int = DEFAULT_HEARTBEAT_INTERVAL,
        scheduler: AsyncIOScheduler | None = None,
    ):
        """Initialize the heartbeat daemon.

        Args:
            interval_minutes: Minutes between heartbeat cycles
            scheduler: Optional shared scheduler instance
        """
        self._interval_minutes = interval_minutes
        self._scheduler = scheduler or AsyncIOScheduler()
        self._owns_scheduler = scheduler is None
        self._running = False
        self._callback: Callable[[str, dict], Any] | None = None

        # Job ID for the main heartbeat job
        self._job_id = "mission_control_heartbeat"

    def start(
        self,
        callback: Callable[[str, dict], Any] | None = None,
    ) -> None:
        """Start the heartbeat daemon.

        Args:
            callback: Optional async callback for heartbeat events
                     Called with (agent_id, event_data)
        """
        if self._running:
            logger.warning("HeartbeatDaemon already running")
            return

        self._callback = callback
        self._running = True

        # Add the heartbeat job
        self._scheduler.add_job(
            self._heartbeat_cycle,
            trigger=IntervalTrigger(minutes=self._interval_minutes),
            id=self._job_id,
            replace_existing=True,
            next_run_time=datetime.now(UTC) + timedelta(seconds=30),  # First run in 30s
        )

        # Start scheduler if we own it
        if self._owns_scheduler and not self._scheduler.running:
            self._scheduler.start()

        logger.info(f"HeartbeatDaemon started (interval: {self._interval_minutes} minutes)")

    def stop(self) -> None:
        """Stop the heartbeat daemon."""
        if not self._running:
            return

        self._running = False

        # Remove our job
        try:
            self._scheduler.remove_job(self._job_id)
        except Exception:
            pass

        # Stop scheduler if we own it
        if self._owns_scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)

        logger.info("HeartbeatDaemon stopped")

    async def _heartbeat_cycle(self) -> None:
        """Execute a heartbeat cycle for all agents.

        Staggered to avoid all agents waking simultaneously.
        """
        manager = get_mission_control_manager()
        agents = await manager.list_agents()

        if not agents:
            logger.debug("HeartbeatDaemon: No agents to wake")
            return

        logger.info(f"HeartbeatDaemon: Starting cycle for {len(agents)} agents")

        # Stagger agent wakeups (2 seconds apart)
        for i, agent in enumerate(agents):
            if not self._running:
                break

            # Wait for stagger delay (except first agent)
            if i > 0:
                await asyncio.sleep(2)

            try:
                await self._wake_agent(agent.id)
            except Exception as e:
                logger.error(f"HeartbeatDaemon: Error waking {agent.name}: {e}")

        logger.info("HeartbeatDaemon: Cycle complete")

    async def _wake_agent(self, agent_id: str) -> None:
        """Wake an individual agent and check for work.

        Args:
            agent_id: ID of agent to wake
        """
        manager = get_mission_control_manager()
        agent = await manager.get_agent(agent_id)

        if not agent:
            return

        logger.debug(f"HeartbeatDaemon: Waking {agent.name}")

        # Check for work
        work_summary = await self._check_for_work(agent_id)

        # Record heartbeat
        await manager.record_heartbeat(agent_id)

        # Update status based on work
        if work_summary["has_urgent_work"]:
            await manager.set_agent_status(agent_id, AgentStatus.ACTIVE)
        elif work_summary["has_work"]:
            await manager.set_agent_status(agent_id, AgentStatus.IDLE)
        else:
            await manager.set_agent_status(agent_id, AgentStatus.IDLE)

        # Fire callback if provided
        if self._callback:
            event_data = {
                "agent_name": agent.name,
                "has_work": work_summary["has_work"],
                "unread_notifications": work_summary["unread_notifications"],
                "assigned_tasks": work_summary["assigned_tasks"],
                "timestamp": datetime.now(UTC).isoformat(),
            }

            try:
                if asyncio.iscoroutinefunction(self._callback):
                    await self._callback(agent_id, event_data)
                else:
                    self._callback(agent_id, event_data)
            except Exception as e:
                logger.error(f"HeartbeatDaemon: Callback error: {e}")

    async def _check_for_work(self, agent_id: str) -> dict[str, Any]:
        """Check what work is available for an agent.

        Returns:
            Dict with work summary:
            - has_work: bool
            - has_urgent_work: bool
            - unread_notifications: int
            - assigned_tasks: int
            - in_progress_tasks: int
        """
        manager = get_mission_control_manager()

        # Get unread notifications
        notifications = await manager.get_notifications_for_agent(agent_id, unread_only=True)
        unread_count = len(notifications)

        # Get assigned tasks
        tasks = await manager.get_tasks_for_agent(agent_id)
        assigned_count = len(tasks)
        in_progress_count = sum(1 for t in tasks if t.status.value == "in_progress")

        # Urgent: unread mentions or tasks waiting
        has_urgent = unread_count > 0
        has_work = assigned_count > 0 or unread_count > 0

        return {
            "has_work": has_work,
            "has_urgent_work": has_urgent,
            "unread_notifications": unread_count,
            "assigned_tasks": assigned_count,
            "in_progress_tasks": in_progress_count,
        }

    async def trigger_heartbeat(self, agent_id: str) -> dict[str, Any]:
        """Manually trigger a heartbeat for a specific agent.

        Useful for immediate updates after task assignments.

        Args:
            agent_id: Agent to wake

        Returns:
            Work summary for the agent
        """
        await self._wake_agent(agent_id)
        return await self._check_for_work(agent_id)

    def set_interval(self, minutes: int) -> None:
        """Change the heartbeat interval.

        Args:
            minutes: New interval in minutes
        """
        self._interval_minutes = minutes

        if self._running:
            # Reschedule job with new interval
            self._scheduler.reschedule_job(
                self._job_id,
                trigger=IntervalTrigger(minutes=minutes),
            )
            logger.info(f"HeartbeatDaemon: Interval changed to {minutes} minutes")


# ============================================================================
# Singleton Management
# ============================================================================

_daemon_instance: HeartbeatDaemon | None = None


def get_heartbeat_daemon(
    interval_minutes: int = DEFAULT_HEARTBEAT_INTERVAL,
    scheduler: AsyncIOScheduler | None = None,
) -> HeartbeatDaemon:
    """Get or create the heartbeat daemon singleton.

    Args:
        interval_minutes: Heartbeat interval (only used on first call)
        scheduler: Optional shared scheduler

    Returns:
        The HeartbeatDaemon instance
    """
    global _daemon_instance
    if _daemon_instance is None:
        _daemon_instance = HeartbeatDaemon(interval_minutes, scheduler)
    return _daemon_instance


def reset_heartbeat_daemon() -> None:
    """Reset the daemon singleton (for testing)."""
    global _daemon_instance
    if _daemon_instance:
        _daemon_instance.stop()
    _daemon_instance = None
