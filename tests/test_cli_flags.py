import pytest
from pocketpaw.__main__ import main


def test_telegram_conflict_discord(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pocketpaw", "--telegram", "--discord"])
    with pytest.raises(SystemExit):
        main()


def test_telegram_conflict_slack(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pocketpaw", "--telegram", "--slack"])
    with pytest.raises(SystemExit):
        main()


def test_telegram_only(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pocketpaw", "--telegram"])
    try:
        main()
    except SystemExit as e:
        # argparse may exit normally, but should not error
        assert e.code == 0 or e.code is None


def test_multi_channel_without_telegram(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pocketpaw", "--discord", "--slack"])
    try:
        main()
    except SystemExit as e:
        assert e.code == 0 or e.code is None
        