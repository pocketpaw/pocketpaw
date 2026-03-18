"""PocketPaw Scheduler - Proactive reminders and scheduled tasks.

Simple reminder system with natural language time parsing.
"""

import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from dateutil import parser as date_parser

from pocketpaw.daemon.triggers import parse_cron_expression


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC).

    Handles legacy naive timestamps stored before UTC migration.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


logger = logging.getLogger(__name__)


class RemindersCorruptError(RuntimeError):
    """Raised when the reminders file exists but cannot be safely parsed."""


def get_reminders_path() -> Path:
    """Get the reminders file path."""
    config_dir = Path.home() / ".pocketpaw"
    config_dir.mkdir(exist_ok=True)
    return config_dir / "reminders.json"


def _quarantine_corrupt_reminders(path: Path) -> Path | None:
    """Move a corrupt reminders file aside so users can recover it manually."""
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S-%f")
    backup = path.with_name(f"{path.name}.corrupt-{timestamp}-{uuid.uuid4().hex[:8]}")
    try:
        path.replace(backup)
        return backup
    except OSError:
        logger.exception("Failed to move corrupt reminders file %s", path)
        return None


def _signal_corrupt_reminders(
    path: Path, reason: str, *, cause: Exception | None = None
) -> NoReturn:
    """Quarantine a corrupt reminders file and raise a typed corruption error."""
    backup = _quarantine_corrupt_reminders(path)
    if backup:
        logger.error("%s in %s; moved to %s for recovery", reason, path, backup)
    else:
        logger.error("%s in %s", reason, path)

    msg = f"{reason} in {path}"
    if cause is not None:
        raise RemindersCorruptError(msg) from cause
    raise RemindersCorruptError(msg)


def _validate_reminder_entry_schema(reminder: object, index: int) -> str | None:
    """Return an error message when a reminder entry is malformed."""
    if not isinstance(reminder, dict):
        return f"Reminder entry at index {index} is not an object"

    reminder_id = reminder.get("id")
    if not isinstance(reminder_id, str) or not reminder_id.strip():
        return f"Reminder entry at index {index} is missing a valid 'id'"

    text = reminder.get("text")
    if not isinstance(text, str) or not text.strip():
        return f"Reminder entry at index {index} is missing a valid 'text'"

    trigger_at = reminder.get("trigger_at")
    if not isinstance(trigger_at, str):
        return f"Reminder entry at index {index} is missing a valid 'trigger_at'"
    try:
        datetime.fromisoformat(trigger_at)
    except (TypeError, ValueError):
        return f"Reminder entry at index {index} has invalid 'trigger_at' format"

    reminder_type = reminder.get("type", "one-shot")
    if reminder_type not in ("one-shot", "recurring"):
        # Unknown types may be valid in future versions — warn and treat as one-shot.
        logger.warning(
            "Reminder entry at index %d has unrecognised 'type': %r — treating as one-shot",
            index,
            reminder_type,
        )

    if reminder_type == "recurring":
        schedule = reminder.get("schedule")
        if not isinstance(schedule, str) or not schedule.strip():
            return f"Recurring reminder at index {index} is missing a valid 'schedule'"

    return None


def load_reminders() -> list[dict]:
    """Load reminders from file."""
    path = get_reminders_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _signal_corrupt_reminders(path, "Corrupt reminders JSON", cause=exc)
        except OSError:
            logger.exception("Failed to read reminders file %s", path)
            return []

        if not isinstance(data, dict):
            _signal_corrupt_reminders(path, "Invalid reminders payload root (expected JSON object)")

        reminders = data.get("reminders", [])
        if not isinstance(reminders, list):
            _signal_corrupt_reminders(path, "Invalid reminders payload (expected list)")

        valid_reminders: list[dict] = []
        for index, reminder in enumerate(reminders):
            validation_error = _validate_reminder_entry_schema(reminder, index)
            if validation_error:
                logger.warning(
                    "Skipping malformed reminder entry at index %d: %s", index, validation_error
                )
            else:
                valid_reminders.append(reminder)

        skipped = len(reminders) - len(valid_reminders)
        if skipped:
            logger.warning(
                "Loaded %d valid reminder(s) from %s; skipped %d malformed entr%s",
                len(valid_reminders),
                path,
                skipped,
                "y" if skipped == 1 else "ies",
            )

        return valid_reminders
    return []


def save_reminders(reminders: list[dict]) -> None:
    """Save reminders to file."""
    path = get_reminders_path()
    data = {"reminders": reminders, "updated_at": datetime.now(tz=UTC).isoformat()}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def parse_natural_time(text: str) -> datetime | None:
    """Parse natural language time expressions.

    Supports:
    - "in X minutes/hours/days/weeks" or "X minutes/hours/days/weeks" (with or without "in")
    - "at HH:MM" or "at H:MM AM/PM"
    - "tomorrow at HH:MM"
    - Absolute dates/times
    """
    text = text.lower().strip()
    now = datetime.now(tz=UTC)

    # Pattern: "in X minutes/hours/days/weeks" or "X minutes/hours/days/weeks" (in is optional)
    pattern = r"(?:in\s+)?(\d+)\s*(minute|min|hour|hr|day|week|second|sec)s?\b"
    relative_match = re.search(pattern, text)

    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2)

        if unit in ("minute", "min"):
            return now + timedelta(minutes=amount)
        elif unit in ("hour", "hr"):
            return now + timedelta(hours=amount)
        elif unit == "day":
            return now + timedelta(days=amount)
        elif unit == "week":
            return now + timedelta(weeks=amount)
        elif unit in ("second", "sec"):
            return now + timedelta(seconds=amount)

    # Pattern: "at HH:MM" or "at H:MM AM/PM"
    at_match = re.search(r"at\s+(\d{1,2}):?(\d{2})?\s*(am|pm)?", text)
    if at_match:
        hour = int(at_match.group(1))
        minute = int(at_match.group(2) or 0)
        period = at_match.group(3)

        if period == "pm" and hour < 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0

        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # Check if "tomorrow" is mentioned
        if "tomorrow" in text:
            target += timedelta(days=1)
        # If time is in the past today, schedule for tomorrow
        elif target <= now:
            target += timedelta(days=1)

        return target

    # Pattern: "tomorrow" (defaults to 9am)
    if "tomorrow" in text and not at_match:
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    # Try dateutil parser for other formats
    try:
        parsed = date_parser.parse(text, fuzzy=True)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        if parsed > now:
            return parsed
    except (ValueError, TypeError):
        pass

    return None


def extract_reminder_text(message: str) -> str:
    """Extract the reminder text from a message.

    E.g., "remind me in 5 minutes to call mom" -> "call mom"
    """
    # Remove common patterns
    patterns = [
        r"^remind\s+me\s+",
        r"in\s+\d+\s*(minute|min|hour|hr|day|week|second|sec)s?\s*",
        r"at\s+\d{1,2}:?\d{0,2}\s*(am|pm)?\s*",
        r"tomorrow\s*",
        r"^to\s+",
    ]

    text = message.lower()
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    # Clean up
    text = text.strip()

    # Capitalize first letter
    if text:
        text = text[0].upper() + text[1:]

    return text or message


class ReminderScheduler:
    """Manages scheduled reminders with APScheduler."""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.reminders: list[dict] = []
        self.callback: Callable | None = None
        self._started = False

    def start(self, callback: Callable | None = None):
        """Start the scheduler and load saved reminders."""
        if self._started:
            return

        self.callback = callback
        load_failed = False
        try:
            self.reminders = load_reminders()
        except RemindersCorruptError:
            # Keep scheduler running, but do not overwrite the corrupt file.
            self.reminders = []
            load_failed = True

        # Schedule self-audit daemon if enabled
        self._schedule_self_audit()

        # Reschedule active reminders
        now = datetime.now(tz=UTC)
        active_reminders = []

        for reminder in self.reminders:
            rtype = reminder.get("type", "one-shot")
            if rtype == "recurring":
                # Recurring reminders are always re-scheduled
                self._add_recurring_job(reminder)
                active_reminders.append(reminder)
            else:
                trigger_time = _ensure_utc(datetime.fromisoformat(reminder["trigger_at"]))
                if trigger_time > now:
                    self._add_job(reminder)
                    active_reminders.append(reminder)
                else:
                    logger.info(f"Skipping past reminder: {reminder['id']}")

        self.reminders = active_reminders
        if load_failed:
            logger.warning(
                "Skipping reminders write on startup because reminders file failed to load"
            )
        else:
            save_reminders(self.reminders)

        self.scheduler.start()
        self._started = True
        logger.info(f"Scheduler started with {len(self.reminders)} reminders")

    def _schedule_self_audit(self) -> None:
        """Schedule the daily self-audit if enabled in settings."""
        try:
            from pocketpaw.config import get_settings

            settings = get_settings()
            if not settings.self_audit_enabled:
                return

            cron_kwargs = parse_cron_expression(settings.self_audit_schedule)

            async def _run_audit():
                from pocketpaw.daemon.self_audit import run_self_audit

                await run_self_audit()

            self.scheduler.add_job(
                _run_audit,
                trigger=CronTrigger(**cron_kwargs),
                id="__self_audit__",
                replace_existing=True,
            )
            logger.info("Self-audit scheduled: %s", settings.self_audit_schedule)
        except Exception as e:
            logger.warning("Failed to schedule self-audit: %s", e)

    def stop(self):
        """Stop the scheduler."""
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False

    async def _trigger_reminder(self, reminder_id: str):
        """Called when a reminder is due."""
        reminder = next((r for r in self.reminders if r["id"] == reminder_id), None)
        if not reminder:
            return

        logger.info(f"Reminder triggered: {reminder['text']}")

        # Call callback if set
        if self.callback:
            await self.callback(reminder)

        # Push to notification channels
        try:
            from pocketpaw.bus.notifier import notify

            await notify(f"Reminder: {reminder['text']}")
        except Exception:
            logger.debug("Notifier dispatch failed for reminder", exc_info=True)

        # Recurring reminders stay; one-shot reminders are removed
        if reminder.get("type", "one-shot") != "recurring":
            self.reminders = [r for r in self.reminders if r["id"] != reminder_id]
            save_reminders(self.reminders)

    def _add_job(self, reminder: dict):
        """Add a scheduler job for a one-shot reminder."""
        trigger_time = _ensure_utc(datetime.fromisoformat(reminder["trigger_at"]))
        self.scheduler.add_job(
            self._trigger_reminder,
            trigger=DateTrigger(run_date=trigger_time),
            args=[reminder["id"]],
            id=reminder["id"],
            replace_existing=True,
        )

    def _add_recurring_job(self, reminder: dict):
        """Add a scheduler job for a recurring reminder."""
        schedule = reminder.get("schedule", "")
        cron_kwargs = parse_cron_expression(schedule)
        self.scheduler.add_job(
            self._trigger_reminder,
            trigger=CronTrigger(**cron_kwargs),
            args=[reminder["id"]],
            id=reminder["id"],
            replace_existing=True,
        )

    def add_reminder(self, message: str) -> dict | None:
        """Add a reminder from a natural language message.

        Args:
            message: Natural language like "remind me in 5 minutes to call mom"

        Returns:
            Reminder dict if successful, None if time couldn't be parsed
        """
        trigger_time = parse_natural_time(message)
        if not trigger_time:
            return None

        reminder_text = extract_reminder_text(message)

        reminder = {
            "id": str(uuid.uuid4()),
            "text": reminder_text,
            "original": message,
            "trigger_at": trigger_time.isoformat(),
            "created_at": datetime.now(tz=UTC).isoformat(),
        }

        self.reminders.append(reminder)
        save_reminders(self.reminders)

        if self._started:
            self._add_job(reminder)

        logger.info(f"Added reminder: {reminder_text} at {trigger_time}")
        return reminder

    def add_recurring(self, message: str, schedule: str) -> dict | None:
        """Add a recurring reminder using a cron expression or preset.

        Args:
            message: Reminder text.
            schedule: Cron expression ("0 8 * * *") or preset name ("every_morning_8am").

        Returns:
            Reminder dict if successful, None if schedule is invalid.
        """
        try:
            parse_cron_expression(schedule)  # validate
        except ValueError as e:
            logger.warning(f"Invalid cron schedule: {e}")
            return None

        reminder = {
            "id": str(uuid.uuid4()),
            "text": message,
            "original": f"recurring: {schedule}",
            "type": "recurring",
            "schedule": schedule,
            "trigger_at": datetime.now(tz=UTC).isoformat(),  # creation time
            "created_at": datetime.now(tz=UTC).isoformat(),
        }

        self.reminders.append(reminder)
        save_reminders(self.reminders)

        if self._started:
            self._add_recurring_job(reminder)

        logger.info(f"Added recurring reminder: {message} [{schedule}]")
        return reminder

    def delete_recurring(self, reminder_id: str) -> bool:
        """Delete a recurring reminder by ID."""
        return self.delete_reminder(reminder_id)

    def delete_reminder(self, reminder_id: str) -> bool:
        """Delete a reminder by ID."""
        reminder = next((r for r in self.reminders if r["id"] == reminder_id), None)
        if not reminder:
            return False

        # Remove from scheduler
        try:
            self.scheduler.remove_job(reminder_id)
        except Exception:
            pass

        # Remove from list
        self.reminders = [r for r in self.reminders if r["id"] != reminder_id]
        save_reminders(self.reminders)

        logger.info(f"Deleted reminder: {reminder_id}")
        return True

    def get_reminders(self) -> list[dict]:
        """Get all active reminders."""
        return self.reminders

    def format_time_remaining(self, reminder: dict) -> str:
        """Format the time remaining for a reminder."""
        trigger_time = _ensure_utc(datetime.fromisoformat(reminder["trigger_at"]))
        delta = trigger_time - datetime.now(tz=UTC)

        if delta.total_seconds() < 0:
            return "past"

        total_seconds = int(delta.total_seconds())

        if total_seconds < 60:
            return f"in {total_seconds}s"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            return f"in {minutes}m"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            if minutes:
                return f"in {hours}h {minutes}m"
            return f"in {hours}h"
        else:
            days = total_seconds // 86400
            return f"in {days}d"


# Singleton instance
_scheduler: ReminderScheduler | None = None


def get_scheduler() -> ReminderScheduler:
    """Get the singleton scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = ReminderScheduler()

        from pocketpaw.lifecycle import register

        def _reset():
            global _scheduler
            _scheduler = None

        register("scheduler", shutdown=_scheduler.stop, reset=_reset)
    return _scheduler
