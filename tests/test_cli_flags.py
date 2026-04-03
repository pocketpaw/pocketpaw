import pytest
from pocketpaw.__main__ import main


def test_telegram_conflict_discord(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pocketpaw", "--telegram", "--discord"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 2


def test_telegram_conflict_slack(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pocketpaw", "--telegram", "--slack"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 2


def test_no_conflict_does_not_raise_argparse_error(monkeypatch):
    monkeypatch.setattr("sys.argv", ["pocketpaw", "--telegram"])
    with pytest.raises(SystemExit) as e:
        main()
    # Should NOT be argparse error
    assert e.value.code != 2