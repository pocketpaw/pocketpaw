"""
TriggerEngine - Manages trigger scheduling for intentions.

Currently supports:
- Cron triggers (via APScheduler CronTrigger)

Future support planned for:
- File watch triggers (watchdog)
- Idle detection triggers
"""

import logging
from collections.abc import Callable
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


# Common cron schedule presets
CRON_PRESETS = {
    "every_minute": "* * * * *",
    "every_5_minutes": "*/5 * * * *",
    "every_15_minutes": "*/15 * * * *",
    "every_30_minutes": "*/30 * * * *",
    "every_hour": "0 * * * *",
    "every_morning_8am": "0 8 * * *",
    "every_morning_9am": "0 9 * * *",
    "weekday_morning_8am": "0 8 * * 1-5",
    "weekday_morning_9am": "0 9 * * 1-5",
    "every_evening_6pm": "0 18 * * *",
    "every_night_10pm": "0 22 * * *",
    "daily_noon": "0 12 * * *",
    "weekly_monday_9am": "0 9 * * 1",
    "monthly_first_9am": "0 9 1 * *",
}


def parse_cron_expression(schedule: str) -> dict:
    """
    Parse a cron expression into APScheduler CronTrigger kwargs.

    Supports:
    - Standard 5-field cron: "minute hour day month day_of_week"
    - Presets: "every_morning_8am", "weekday_morning_9am", etc.

    Args:
        schedule: Cron expression or preset name

    Returns:
        Dict of kwargs for CronTrigger
    """
    # Check if it's a preset
    if schedule in CRON_PRESETS:
        schedule = CRON_PRESETS[schedule]

    parts = schedule.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: {schedule}. Expected 5 fields.")

    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        "day_of_week": parts[4],
    }


class TriggerEngine:
    """
    Manages trigger scheduling for intentions using APScheduler.

    Each intention gets a job added to the scheduler. When the trigger fires,
    the callback is invoked with the intention data.
    """

    def __init__(self, scheduler: AsyncIOScheduler | None = None):
        """
        Initialize the trigger engine.

        Args:
            scheduler: Optional existing scheduler to use.
                       If not provided, creates a new one.
        """
        self._own_scheduler = scheduler is None
        self.scheduler = scheduler or AsyncIOScheduler()
        self.callback: Callable | None = None
        self._jobs: dict[str, str] = {}  # intention_id -> job_id

    def start(self, callback: Callable) -> None:
        """
        Start the trigger engine.

        Args:
            callback: Async function to call when a trigger fires.
                      Signature: async def callback(intention: dict)
        """
        self.callback = callback

        if self._own_scheduler and not self.scheduler.running:
            self.scheduler.start()
            logger.info("TriggerEngine scheduler started")

    def stop(self) -> None:
        """Stop the trigger engine and remove all jobs."""
        self.remove_all_jobs()

        if self._own_scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("TriggerEngine scheduler stopped")

    def add_intention(self, intention: dict) -> bool:
        """
        Add trigger for an intention.

        Args:
            intention: Intention dict with trigger configuration

        Returns:
            True if trigger was added successfully
        """
        if not intention.get("enabled", True):
            logger.debug(f"Skipping disabled intention: {intention['name']}")
            return False

        trigger_config = intention.get("trigger", {})
        trigger_type = trigger_config.get("type")

        if trigger_type == "cron":
            return self._add_cron_trigger(intention)
        else:
            logger.warning(f"Unknown trigger type: {trigger_type}")
            return False

    def _add_cron_trigger(self, intention: dict) -> bool:
        """Add a cron-based trigger for an intention."""
        intention_id = intention["id"]
        trigger_config = intention["trigger"]
        schedule = trigger_config.get("schedule")

        if not schedule:
            logger.error(f"No schedule in trigger config for {intention['name']}")
            return False

        try:
            cron_kwargs = parse_cron_expression(schedule)
            trigger = CronTrigger(**cron_kwargs)

            job_id = f"intention_{intention_id}"

            # Remove existing job if any
            if intention_id in self._jobs:
                self.remove_intention(intention_id)

            # Add new job
            self.scheduler.add_job(
                self._fire_trigger,
                trigger=trigger,
                args=[intention],
                id=job_id,
                replace_existing=True,
                name=intention["name"],
            )

            self._jobs[intention_id] = job_id
            logger.info(f"Added cron trigger for '{intention['name']}': {schedule}")
            return True

        except Exception as e:
            logger.error(f"Failed to add cron trigger for {intention['name']}: {e}")
            return False

    async def _fire_trigger(self, intention: dict) -> None:
        """Called when a trigger fires."""
        logger.info(f"Trigger fired for intention: {intention['name']}")

        if self.callback:
            try:
                await self.callback(intention)
            except Exception as e:
                logger.error(f"Error executing intention {intention['name']}: {e}")

    def remove_intention(self, intention_id: str) -> bool:
        """
        Remove trigger for an intention.

        Args:
            intention_id: ID of the intention to remove

        Returns:
            True if trigger was removed
        """
        job_id = self._jobs.get(intention_id)
        if job_id:
            try:
                self.scheduler.remove_job(job_id)
                del self._jobs[intention_id]
                logger.info(f"Removed trigger for intention: {intention_id}")
                return True
            except Exception as e:
                logger.error(f"Failed to remove trigger {intention_id}: {e}")

        return False

    def remove_all_jobs(self) -> None:
        """Remove all intention triggers."""
        for intention_id in list(self._jobs.keys()):
            self.remove_intention(intention_id)

    def update_intention(self, intention: dict) -> bool:
        """
        Update trigger for an intention (remove and re-add).

        Args:
            intention: Updated intention dict

        Returns:
            True if updated successfully
        """
        self.remove_intention(intention["id"])
        return self.add_intention(intention)

    def get_next_run_time(self, intention_id: str) -> datetime | None:
        """Get the next scheduled run time for an intention."""
        job_id = self._jobs.get(intention_id)
        if job_id:
            job = self.scheduler.get_job(job_id)
            if job and job.next_run_time:
                return job.next_run_time
        return None

    def get_scheduled_intentions(self) -> list[str]:
        """Get list of intention IDs with active triggers."""
        return list(self._jobs.keys())

    def run_now(self, intention: dict) -> None:
        """
        Manually trigger an intention immediately.

        Args:
            intention: Intention to run
        """
        if self.callback:
            import asyncio

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._fire_trigger(intention))
                else:
                    loop.run_until_complete(self._fire_trigger(intention))
            except RuntimeError:
                # No event loop running
                asyncio.run(self._fire_trigger(intention))
