"""Tests for load_reminders() corrupt JSON handling - issue #659."""

import json
from unittest.mock import patch

from pocketpaw.scheduler import load_reminders


class TestLoadRemindersCorruptJSON:
    """Tests for fix: issue #659 - silent data loss on corrupt reminders.json."""

    def test_valid_json_returns_reminders(self, tmp_path):
        """Valid reminders.json should return the reminders list."""
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps({"reminders": [{"id": "1", "text": "test"}]}))

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            result = load_reminders()

        assert result == [{"id": "1", "text": "test"}]

    def test_corrupt_json_returns_empty_list(self, tmp_path):
        """Corrupt reminders.json should return empty list, not raise."""
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text("{bad json:")

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            result = load_reminders()

        assert result == []

    def test_corrupt_json_logs_warning(self, tmp_path, caplog):
        """Corrupt reminders.json should log a warning."""
        import logging

        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text("{bad json:")

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            with caplog.at_level(logging.WARNING):
                load_reminders()

        assert any("corrupted" in record.message.lower() for record in caplog.records)

    def test_corrupt_json_creates_bak_file(self, tmp_path):
        """Corrupt reminders.json should be renamed to reminders.json.bak."""
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text("{bad json:")

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            load_reminders()

        assert (tmp_path / "reminders.json.bak").exists()
        assert not reminders_file.exists()

    def test_missing_file_returns_empty_list(self, tmp_path):
        """Missing reminders.json should return empty list silently."""
        reminders_file = tmp_path / "reminders.json"

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            result = load_reminders()

        assert result == []

    def test_empty_reminders_key_returns_empty_list(self, tmp_path):
        """Valid JSON with empty reminders list should return empty list."""
        reminders_file = tmp_path / "reminders.json"
        reminders_file.write_text(json.dumps({"reminders": []}))

        with patch("pocketpaw.scheduler.get_reminders_path", return_value=reminders_file):
            result = load_reminders()

        assert result == []
