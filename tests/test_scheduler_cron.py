# Tests for Feature 5: Cron Expression Support in scheduler
# Created: 2026-02-06

import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pocketpaw.scheduler import ReminderScheduler, RemindersCorruptError, load_reminders


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

    @patch("pocketpaw.scheduler.save_reminders")
    @patch("pocketpaw.scheduler.load_reminders", return_value=[])
    def test_add_recurring_valid_cron(self, mock_load, mock_save, scheduler):
        result = scheduler.add_recurring("Daily standup", "0 9 * * *")

        assert result is not None
        assert result["type"] == "recurring"
        assert result["schedule"] == "0 9 * * *"
        assert result["text"] == "Daily standup"
        assert "id" in result
        mock_save.assert_called()

    @patch("pocketpaw.scheduler.save_reminders")
    @patch("pocketpaw.scheduler.load_reminders", return_value=[])
    def test_add_recurring_preset(self, mock_load, mock_save, scheduler):
        result = scheduler.add_recurring("Morning check", "every_morning_8am")

        assert result is not None
        assert result["schedule"] == "every_morning_8am"
        assert result["type"] == "recurring"

    @patch("pocketpaw.scheduler.save_reminders")
    @patch("pocketpaw.scheduler.load_reminders", return_value=[])
    def test_add_recurring_invalid_cron(self, mock_load, mock_save, scheduler):
        result = scheduler.add_recurring("Bad schedule", "not a cron")

        assert result is None

    @patch("pocketpaw.scheduler.save_reminders")
    @patch("pocketpaw.scheduler.load_reminders", return_value=[])
    def test_add_recurring_appended_to_reminders(self, mock_load, mock_save, scheduler):
        scheduler.add_recurring("Task A", "0 8 * * *")
        scheduler.add_recurring("Task B", "0 12 * * *")

        assert len(scheduler.reminders) == 2
        assert scheduler.reminders[0]["text"] == "Task A"
        assert scheduler.reminders[1]["text"] == "Task B"

    @patch("pocketpaw.scheduler.save_reminders")
    @patch("pocketpaw.scheduler.load_reminders", return_value=[])
    def test_delete_recurring(self, mock_load, mock_save, scheduler):
        reminder = scheduler.add_recurring("To delete", "0 9 * * *")
        assert len(scheduler.reminders) == 1

        result = scheduler.delete_recurring(reminder["id"])
        assert result is True
        assert len(scheduler.reminders) == 0

    @patch("pocketpaw.scheduler.save_reminders")
    @patch("pocketpaw.scheduler.load_reminders", return_value=[])
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

        with patch("pocketpaw.scheduler.save_reminders"):
            with patch("pocketpaw.scheduler.load_reminders", return_value=[]):
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

        with patch("pocketpaw.scheduler.save_reminders"):
            with patch("pocketpaw.scheduler.load_reminders", return_value=[]):
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

    @patch("pocketpaw.scheduler.save_reminders")
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

        with patch("pocketpaw.scheduler.load_reminders", return_value=[recurring_reminder]):
            scheduler = ReminderScheduler()
            scheduler.start()

            # Recurring reminder should still be active (not skipped)
            assert len(scheduler.reminders) == 1
            assert scheduler.reminders[0]["id"] == "recurring-123"

            scheduler.stop()

    @patch("pocketpaw.scheduler.save_reminders")
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

        with patch("pocketpaw.scheduler.load_reminders", return_value=[oneshot_reminder]):
            scheduler = ReminderScheduler()
            scheduler.start()

            # Should have been skipped
            assert len(scheduler.reminders) == 0

            scheduler.stop()


class TestReminderFileCorruption:
    def test_load_reminders_quarantines_corrupt_json(self, tmp_path):
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text("{bad json", encoding="utf-8")

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            with pytest.raises(RemindersCorruptError):
                load_reminders()

        backups = list(tmp_path.glob("reminders.json.corrupt-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "{bad json"
        assert not reminders_file.exists()

    @patch("pocketpaw.scheduler.save_reminders")
    @patch("pocketpaw.scheduler.load_reminders", side_effect=RemindersCorruptError("bad json"))
    def test_start_does_not_overwrite_when_load_is_corrupt(self, mock_load, mock_save):
        scheduler = ReminderScheduler()
        with patch.object(scheduler.scheduler, "start"):
            scheduler.start()
        assert scheduler.reminders == []
        mock_save.assert_not_called()

    def test_load_reminders_quarantines_non_dict_root(self, tmp_path):
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text("[]", encoding="utf-8")

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            with pytest.raises(RemindersCorruptError):
                load_reminders()

        backups = list(tmp_path.glob("reminders.json.corrupt-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "[]"
        assert not reminders_file.exists()

    def test_load_reminders_quarantines_non_utf8_bytes(self, tmp_path):
        reminders_file = tmp_path / "reminders.json"
        raw = b"\xff\xfe\xfa"
        reminders_file.write_bytes(raw)

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            with pytest.raises(RemindersCorruptError):
                load_reminders()

        backups = list(tmp_path.glob("reminders.json.corrupt-*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == raw
        assert not reminders_file.exists()

    def test_load_reminders_quarantines_invalid_reminder_entry_schema(self, tmp_path):
        """A fully-corrupt list root (a bare string) still quarantines."""
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(
            json.dumps({"reminders": ["oops"], "updated_at": datetime.now().isoformat()}),
            encoding="utf-8",
        )

        # "oops" is not a dict — malformed entry is skipped, file is NOT quarantined
        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            result = load_reminders()

        assert result == []
        # File should still be present (not quarantined — partial corruption only skips entries)
        assert reminders_file.exists()
        backups = list(tmp_path.glob("reminders.json.corrupt-*"))
        assert len(backups) == 0

    def test_load_reminders_partial_corruption_keeps_valid_entries(self, tmp_path):
        """Valid entries are kept when only some entries are malformed."""
        good_entry = {
            "id": "good-1",
            "text": "Valid reminder",
            "trigger_at": "2099-01-01T09:00:00",
            "type": "one-shot",
        }
        bad_entry = {"id": "", "text": ""}  # missing valid id and text

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(
            json.dumps(
                {"reminders": [good_entry, bad_entry], "updated_at": datetime.now().isoformat()}
            ),
            encoding="utf-8",
        )

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            result = load_reminders()

        assert len(result) == 1
        assert result[0]["id"] == "good-1"
        assert reminders_file.exists()

    def test_load_reminders_oserror_returns_empty_list(self, tmp_path):
        """OSError on read returns an empty list without raising."""
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text("{}", encoding="utf-8")

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            with patch.object(
                reminders_file.__class__, "read_text", side_effect=OSError("disk error")
            ):
                result = load_reminders()

        assert result == []

    def test_load_reminders_quarantine_failure_still_raises(self, tmp_path):
        """Even if the quarantine move fails, RemindersCorruptError is still raised."""
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text("{bad json", encoding="utf-8")

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            with patch("pocketpaw.scheduler._quarantine_corrupt_reminders", return_value=None):
                with pytest.raises(RemindersCorruptError):
                    load_reminders()

    def test_load_reminders_skips_recurring_missing_schedule(self, tmp_path):
        """Recurring reminder missing 'schedule' is skipped, not quarantined."""
        bad_recurring = {
            "id": "bad-recurring",
            "text": "No schedule",
            "trigger_at": "2099-01-01T09:00:00",
            "type": "recurring",
            # 'schedule' field intentionally omitted
        }
        good_entry = {
            "id": "good-1",
            "text": "Valid reminder",
            "trigger_at": "2099-01-01T09:00:00",
            "type": "one-shot",
        }

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(
            json.dumps(
                {
                    "reminders": [bad_recurring, good_entry],
                    "updated_at": datetime.now().isoformat(),
                }
            ),
            encoding="utf-8",
        )

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            result = load_reminders()

        assert len(result) == 1
        assert result[0]["id"] == "good-1"
        assert reminders_file.exists()

    def test_load_reminders_creates_unique_quarantine_files(self, tmp_path):
        reminders_file = tmp_path / "reminders.json"
        for _ in range(2):
            reminders_file.write_text("{bad json", encoding="utf-8")
            with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
                with pytest.raises(RemindersCorruptError):
                    load_reminders()

        backups = list(tmp_path.glob("reminders.json.corrupt-*"))
        assert len(backups) == 2
