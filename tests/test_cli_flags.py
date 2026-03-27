import pytest
from pocketpaw.__main__ import main

def test_telegram_conflict(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pocketpaw", "--telegram", "--discord"])
    with pytest.raises(SystemExit):
        main()