"""Tests for CLI flag validation in pocketpaw.__main__."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("conflict_flag", ["--discord", "--slack", "--whatsapp"])
def test_telegram_conflicts_with_other_channel_flag(monkeypatch, conflict_flag):
    """--telegram combined with another channel flag must exit via argparse error (code 2)."""
    from pocketpaw.__main__ import main

    monkeypatch.setattr("sys.argv", ["pocketpaw", "--telegram", conflict_flag])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2
