"""
TriggerEngine - Manages trigger scheduling for intentions.

Currently supports:
- Cron triggers (via APScheduler CronTrigger)
- Stale-session triggers (interval polling of session last_activity)

Future support planned for:
- File watch triggers (watchdog)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..memory.manager import get_memory_manager

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


# Default stale-session check interval (minutes) and threshold (hours)
_DEFAULT_STALE_CHECK_MINUTES = 60
_DEFAULT_STALE_THRESHOLD_HOURS = 12


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
        # session_key -> last nudge time (rate-limit stale nudges per session)
        self._nudged_sessions: dict[str, datetime] = {}

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
        elif trigger_type == "stale":
            return self._add_stale_trigger(intention)
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

    def _add_stale_trigger(self, intention: dict) -> bool:
        """Add an interval-based stale-session trigger for an intention."""
        intention_id = intention["id"]
        trigger_config = intention["trigger"]

        check_minutes = trigger_config.get("check_interval_minutes", _DEFAULT_STALE_CHECK_MINUTES)
        job_id = f"intention_{intention_id}"

        # Remove existing job if any
        if intention_id in self._jobs:
            self.remove_intention(intention_id)

        try:
            self.scheduler.add_job(
                self._fire_stale_trigger,
                trigger=IntervalTrigger(minutes=check_minutes),
                args=[intention],
                id=job_id,
                replace_existing=True,
                name=intention["name"],
            )
            self._jobs[intention_id] = job_id
            logger.info(
                f"Added stale trigger for '{intention['name']}': checks every {check_minutes} min"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to add stale trigger for {intention['name']}: {e}")
            return False

    async def _fire_stale_trigger(self, intention: dict) -> None:
        """Scan session index and fire for each stale session (rate-limited)."""
        if not self.callback:
            return

        trigger_config = intention.get("trigger", {})
        threshold_hours = trigger_config.get("threshold_hours", _DEFAULT_STALE_THRESHOLD_HOURS)
        # Re-nudge window: don't fire the same session again within 2× the threshold
        cooldown = timedelta(hours=threshold_hours * 2)
        cutoff = datetime.now(tz=UTC) - timedelta(hours=threshold_hours)

        # Evict expired entries so _nudged_sessions doesn't grow unboundedly
        now_evict = datetime.now(tz=UTC)
        expired_keys = [
            k for k, ts in self._nudged_sessions.items() if (now_evict - ts) >= cooldown
        ]
        for k in expired_keys:
            del self._nudged_sessions[k]

        stale_sessions = self._find_stale_sessions(cutoff)
        if not stale_sessions:
            logger.debug(f"[stale trigger] No stale sessions found for '{intention['name']}'")
            return

        now = datetime.now(tz=UTC)
        for session_meta in stale_sessions:
            session_key = session_meta["session_key"]
            last_nudge = self._nudged_sessions.get(session_key)
            if last_nudge and (now - last_nudge) < cooldown:
                logger.debug(f"[stale trigger] Skipping {session_key!r} — nudged recently")
                continue

            self._nudged_sessions[session_key] = now
            enriched = {**intention, "_stale_session": session_meta}
            logger.info(
                f"[stale trigger] Firing for session {session_key!r} "
                f"('{session_meta.get('title', 'untitled')}'), "
                f"idle ~{session_meta.get('idle_hours', '?')}h"
            )
            try:
                await self.callback(enriched)
            except Exception as e:
                logger.error(
                    f"Error executing stale intention '{intention['name']}' "
                    f"for session {session_key!r}: {e}"
                )

    def _find_stale_sessions(self, cutoff: datetime) -> list[dict]:
        """
        Return session metadata dicts for sessions with last_activity before *cutoff*.

        Reads the file-store session index if available; falls back to an empty list
        so the trigger degrades gracefully for other memory backends.
        """
        try:
            index: dict = get_memory_manager().list_sessions_with_metadata()
        except Exception as e:
            logger.warning(f"[stale trigger] Could not read session index: {e}")
            return []

        stale = []
        now = datetime.now(tz=UTC)
        for safe_key, meta in index.items():
            last_activity_str = meta.get("last_activity", "")
            if not last_activity_str:
                continue
            try:
                last_activity = datetime.fromisoformat(last_activity_str)
                # Normalise to UTC if the stored value is naive
                if last_activity.tzinfo is None:
                    last_activity = last_activity.replace(tzinfo=UTC)
            except ValueError:
                continue

            if last_activity < cutoff:
                idle_hours = round((now - last_activity).total_seconds() / 3600, 1)
                stale.append(
                    {
                        "session_key": safe_key,
                        "title": meta.get("title", "New Chat"),
                        "last_activity": last_activity_str,
                        "idle_hours": idle_hours,
                        "preview": meta.get("preview", ""),
                    }
                )
        return stale

    async def _fire_trigger(self, intention: dict) -> None:
        """Called when a cron trigger fires."""
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
                asyncio.get_running_loop()
            except RuntimeError:
                # No event loop running — safe to use asyncio.run()
                asyncio.run(self._fire_trigger(intention))
            else:
                # Already inside a running loop (e.g. pytest-asyncio) — schedule, don't block
                asyncio.create_task(self._fire_trigger(intention))
