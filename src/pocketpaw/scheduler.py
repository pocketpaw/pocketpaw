"""PocketPaw Scheduler - Proactive reminders and scheduled tasks.

Simple reminder system with natural language time parsing.
"""

import json
import logging
import re
from string import Formatter
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def get_reminders_path() -> Path:
    """Get the reminders file path."""
    config_dir = Path.home() / ".pocketpaw"
    config_dir.mkdir(exist_ok=True)
    return config_dir / "reminders.json"


def load_reminders() -> list[dict]:
    """Load reminders from file."""
    path = get_reminders_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return data.get("reminders", [])
        except (json.JSONDecodeError, Exception):
            pass
    return []


def save_reminders(reminders: list[dict]) -> None:
    """Save reminders to file."""
    path = get_reminders_path()
    data = {"reminders": reminders, "updated_at": datetime.now(tz=UTC).isoformat()}
    path.write_text(json.dumps(data, indent=2))


def parse_natural_time(text: str) -> datetime | None:
    """Parse natural language time expressions.

    Supports:
    - "in X minutes/hours/days"
    - "at HH:MM" or "at H:MM AM/PM"
    - "tomorrow at HH:MM"
    - Absolute dates/times
    """
    text = text.lower().strip()
    now = datetime.now(tz=UTC)

    # Pattern: "in X minutes/hours/days"
    relative_match = re.search(r"in\s+(\d+)\s*(minute|min|hour|hr|day|second|sec)s?", text)
    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2)

        if unit in ("minute", "min"):
            return now + timedelta(minutes=amount)
        elif unit in ("hour", "hr"):
            return now + timedelta(hours=amount)
        elif unit == "day":
            return now + timedelta(days=amount)
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
        r"in\s+\d+\s*(minute|min|hour|hr|day|second|sec)s?\s*",
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

def _render_template(template: str, variables: dict) -> str:
    """Render template string leaving unknown placeholders intact.
    
    e.g. "Hello {name}, take {medicine}" + {"name": "Mom"}
      -> "Hello Mom, take {medicine}"
    """
    result = []
    for literal_text, field_name, format_spec, _ in Formatter().parse(template):
        result.append(literal_text)
        if field_name is not None:
            value = variables.get(field_name, f"{{{field_name}}}")
            result.append(format(value, format_spec) if format_spec else str(value))
    return "".join(result)


def _log_routine_send(entry_id: str, recipient: str, recipient_name: str, message: str, status: str = "sent"):
    """Append a send event to ~/.pocketpaw/routine_history.jsonl"""
    import json
    import uuid
    from datetime import datetime, timezone
    from pathlib import Path

    history_path = Path.home() / ".pocketpaw" / "routine_history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "id": str(uuid.uuid4()),
        "entry_id": entry_id,
        "recipient": recipient,
        "recipient_name": recipient_name,
        "message": message,
        "status": status,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(history_path, "a") as f:
        f.write(json.dumps(record) + "\n")


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
        self.reminders = load_reminders()

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
    
    # ── Scheduled Messages (Routines) ────────────────────────────────────────

    def load_scheduled_message_jobs(self) -> None:
        """Load all scheduled messages from config and register APScheduler jobs."""
        from pocketpaw.config import Settings
        try:
            settings = Settings.load()
            for entry in settings.scheduled_messages:
                if entry.get("enabled", True):
                    self._add_scheduled_message_job(entry)
            logger.info(f"Loaded {len(settings.scheduled_messages)} scheduled message jobs")
        except Exception as e:
            logger.error(f"Failed to load scheduled messages: {e}")

    def _add_scheduled_message_job(self, entry: dict) -> None:
        """Register a single scheduled message job with APScheduler."""
        from apscheduler.triggers.cron import CronTrigger
        try:
            entry_id = entry["id"]
            job_id = f"sched_msg:{entry_id}"
            trigger = CronTrigger.from_crontab(
                entry["schedule"],
                timezone=entry.get("timezone", "UTC")
            )
           
            self.scheduler.add_job(
                self._fire_scheduled_message,
                trigger=trigger,
                args=[entry_id],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=60,
            )
            logger.debug(f"Registered scheduled message job: {job_id}")
        except Exception as e:
            logger.error(f"Failed to register job for {entry.get('id')}: {e}")

    async def _fire_scheduled_message(self, entry_id: str) -> None:
        """APScheduler callback — renders template and sends via MessageBus."""
        try:
            from pocketpaw.config import Settings
            entries = Settings.load().scheduled_messages
        except Exception as e:
            logger.error("Could not load config on fire: %s", e)
            return

        entry = next((e for e in entries if e.get("id") == entry_id), None)
        if not entry:
            logger.warning("Entry %s not found in config — was it deleted?", entry_id)
            return

        if not entry.get("enabled", True):
            return

        # Render template
        variables = {"name": entry.get("recipient_name", ""), **entry.get("variables", {})}
        message = _render_template(entry["template"], variables)

        # Route via MessageBus
        try:
            from pocketpaw.bus import get_message_bus
            from pocketpaw.bus.events import Channel, OutboundMessage

            bus = get_message_bus()
            await bus.publish_outbound(
                OutboundMessage(
                    channel=Channel(entry["channel"]),
                    chat_id=entry["recipient"],
                    content=message,
                )
            )
            _log_routine_send(entry_id, entry["recipient"], entry.get("recipient_name",""), message, "sent")
            logger.info(
                "Fired scheduled message → %s (%s) via %s",
                entry["recipient"], entry.get("recipient_name"), entry["channel"],
            )
        except Exception as e:
            _log_routine_send(entry_id, entry["recipient"], entry.get("recipient_name",""), message, "failed")
            logger.error("Failed to fire scheduled message %s: %s", entry_id, e)

    def reload_scheduled_message_jobs(self) -> None:
        """Remove all sched_msg:* jobs and re-register from config."""
        for job in self.scheduler.get_jobs():
            if job.id.startswith("sched_msg:"):
                job.remove()
        self.load_scheduled_message_jobs()

    def add_scheduled_message(self, data: dict) -> dict:
        """Persist a new scheduled message and register its APScheduler job."""
        from pocketpaw.config import Settings
        data.setdefault("id", str(uuid.uuid4()))
        data.setdefault("title", "")
        data.setdefault("enabled", True)
        data.setdefault("timezone", "UTC")
        data.setdefault("variables", {})
        data.setdefault("recipient_name", "")

        settings = Settings.load()
        settings.scheduled_messages.append(data)
        settings.save()

        if self._started and data["enabled"]:
            self._add_scheduled_message_job(data)
        return data

    def update_scheduled_message(self, message_id: str, updates: dict) -> dict | None:
        """Update an existing scheduled message and re-register its job."""
        from pocketpaw.config import Settings
        settings = Settings.load()
        for i, entry in enumerate(settings.scheduled_messages):
            if entry.get("id") == message_id:
                entry.update(updates)
                entry["id"] = message_id
                settings.scheduled_messages[i] = entry
                settings.save()
                if self._started:
                    self.reload_scheduled_message_jobs()
                return entry
        return None

    def delete_scheduled_message(self, message_id: str) -> bool:
        """Delete a scheduled message and remove its job."""
        from pocketpaw.config import Settings
        settings = Settings.load()
        original_count = len(settings.scheduled_messages)
        settings.scheduled_messages = [
            e for e in settings.scheduled_messages if e.get("id") != message_id
        ]
        if len(settings.scheduled_messages) < original_count:
            settings.save()
            job_id = f"sched_msg:{message_id}"
            job = self.scheduler.get_job(job_id)
            if job:
                job.remove()
            return True
        return False

    def toggle_scheduled_message(self, message_id: str) -> dict | None:
        """Toggle enabled state of a scheduled message."""
        from pocketpaw.config import Settings
        settings = Settings.load()
        for i, entry in enumerate(settings.scheduled_messages):
            if entry.get("id") == message_id:
                entry["enabled"] = not entry.get("enabled", True)
                settings.scheduled_messages[i] = entry
                settings.save()
                if self._started:
                    self.reload_scheduled_message_jobs()
                return entry
        return None

    def get_scheduled_messages(self) -> list[dict]:
        """Return all scheduled messages from config."""
        from pocketpaw.config import Settings
        try:
            return Settings.load().scheduled_messages
        except Exception:
            return []

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
