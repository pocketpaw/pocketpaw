# Tests for Feature 5: Cron Expression Support in scheduler
# Created: 2026-02-06

import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pocketclaw.scheduler import ReminderScheduler


@pytest.fixture
def scheduler():
    return ReminderScheduler()


@pytest.fixture
def temp_reminders_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"reminders": [], "updated_at": datetime.now().isoformat()}, f)
        yield Path(f.name)


class TestCronExpressionSupport:
    """Tests for recurring cron reminders in ReminderScheduler."""

    @patch("pocketclaw.scheduler.save_reminders")
    @patch("pocketclaw.scheduler.load_reminders", return_value=[])
    def test_add_recurring_valid_cron(self, mock_load, mock_save, scheduler):
        result = scheduler.add_recurring("Daily standup", "0 9 * * *")

        assert result is not None
        assert result["type"] == "recurring"
        assert result["schedule"] == "0 9 * * *"
        assert result["text"] == "Daily standup"
        assert "id" in result
        mock_save.assert_called()

    @patch("pocketclaw.scheduler.save_reminders")
    @patch("pocketclaw.scheduler.load_reminders", return_value=[])
    def test_add_recurring_preset(self, mock_load, mock_save, scheduler):
        result = scheduler.add_recurring("Morning check", "every_morning_8am")

        assert result is not None
        assert result["schedule"] == "every_morning_8am"
        assert result["type"] == "recurring"

    @patch("pocketclaw.scheduler.save_reminders")
    @patch("pocketclaw.scheduler.load_reminders", return_value=[])
    def test_add_recurring_invalid_cron(self, mock_load, mock_save, scheduler):
        result = scheduler.add_recurring("Bad schedule", "not a cron")

        assert result is None

    @patch("pocketclaw.scheduler.save_reminders")
    @patch("pocketclaw.scheduler.load_reminders", return_value=[])
    def test_add_recurring_appended_to_reminders(self, mock_load, mock_save, scheduler):
        scheduler.add_recurring("Task A", "0 8 * * *")
        scheduler.add_recurring("Task B", "0 12 * * *")

        assert len(scheduler.reminders) == 2
        assert scheduler.reminders[0]["text"] == "Task A"
        assert scheduler.reminders[1]["text"] == "Task B"

    @patch("pocketclaw.scheduler.save_reminders")
    @patch("pocketclaw.scheduler.load_reminders", return_value=[])
    def test_delete_recurring(self, mock_load, mock_save, scheduler):
        reminder = scheduler.add_recurring("To delete", "0 9 * * *")
        assert len(scheduler.reminders) == 1

        result = scheduler.delete_recurring(reminder["id"])
        assert result is True
        assert len(scheduler.reminders) == 0

    @patch("pocketclaw.scheduler.save_reminders")
    @patch("pocketclaw.scheduler.load_reminders", return_value=[])
    def test_recurring_reminder_has_correct_fields(self, mock_load, mock_save, scheduler):
        result = scheduler.add_recurring("Weekly sync", "0 10 * * 1")

        assert "id" in result
        assert "text" in result
        assert "type" in result
        assert "schedule" in result
        assert "trigger_at" in result
        assert "created_at" in result
        assert result["original"] == "recurring: 0 10 * * 1"

    async def test_recurring_reminder_not_removed_on_trigger(self, scheduler):
        """Recurring reminders should NOT be removed after firing."""
        callback = AsyncMock()

        with patch("pocketclaw.scheduler.save_reminders"):
            with patch("pocketclaw.scheduler.load_reminders", return_value=[]):
                scheduler.callback = callback
                reminder = scheduler.add_recurring("Keep me", "0 9 * * *")
                rid = reminder["id"]

                # Simulate trigger
                await scheduler._trigger_reminder(rid)

                # Should still be in the list
                assert any(r["id"] == rid for r in scheduler.reminders)
                callback.assert_called_once()

    async def test_oneshot_reminder_removed_on_trigger(self, scheduler):
        """One-shot reminders should be removed after firing."""
        callback = AsyncMock()

        with patch("pocketclaw.scheduler.save_reminders"):
            with patch("pocketclaw.scheduler.load_reminders", return_value=[]):
                scheduler.callback = callback

                # Add a one-shot reminder manually
                reminder = {
                    "id": "test-oneshot",
                    "text": "Remove me",
                    "original": "test",
                    "type": "one-shot",
                    "trigger_at": datetime.now().isoformat(),
                    "created_at": datetime.now().isoformat(),
                }
                scheduler.reminders.append(reminder)

                await scheduler._trigger_reminder("test-oneshot")

                # Should be removed
                assert not any(r["id"] == "test-oneshot" for r in scheduler.reminders)

    @patch("pocketclaw.scheduler.save_reminders")
    async def test_start_reschedules_recurring(self, mock_save):
        """Recurring reminders should be rescheduled on start."""
        recurring_reminder = {
            "id": "recurring-123",
            "text": "Daily task",
            "original": "recurring: 0 9 * * *",
            "type": "recurring",
            "schedule": "0 9 * * *",
            "trigger_at": "2026-01-01T09:00:00",  # Past date
            "created_at": "2026-01-01T00:00:00",
        }

        with patch("pocketclaw.scheduler.load_reminders", return_value=[recurring_reminder]):
            scheduler = ReminderScheduler()
            scheduler.start()

            # Recurring reminder should still be active (not skipped)
            assert len(scheduler.reminders) == 1
            assert scheduler.reminders[0]["id"] == "recurring-123"

            scheduler.stop()

    @patch("pocketclaw.scheduler.save_reminders")
    async def test_start_skips_past_oneshot(self, mock_save):
        """One-shot reminders in the past should be skipped on start."""
        oneshot_reminder = {
            "id": "oneshot-123",
            "text": "Past task",
            "original": "test",
            "type": "one-shot",
            "trigger_at": "2020-01-01T09:00:00",  # Past date
            "created_at": "2020-01-01T00:00:00",
        }

        with patch("pocketclaw.scheduler.load_reminders", return_value=[oneshot_reminder]):
            scheduler = ReminderScheduler()
            scheduler.start()

            # Should have been skipped
            assert len(scheduler.reminders) == 0

            scheduler.stop()
