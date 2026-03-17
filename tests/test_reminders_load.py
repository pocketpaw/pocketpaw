import json
from pathlib import Path

from pocketpaw.scheduler import load_reminders


def test_load_reminders_with_corrupted_file(tmp_path, monkeypatch):
    # Create fake .pocketpaw directory
    fake_home = tmp_path
    config_dir = fake_home / ".pocketpaw"
    config_dir.mkdir()

    # Create corrupted reminders.json
    reminders_file = config_dir / "reminders.json"
    reminders_file.write_text("{ invalid json")

    # Monkeypatch Path.home() to use temp directory
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    # Run function
    result = load_reminders()

    # Assert safe fallback
    assert result == []

    # Assert backup created
    backup_file = config_dir / "reminders.backup.json"
    assert backup_file.exists()