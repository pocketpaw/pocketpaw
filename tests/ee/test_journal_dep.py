# tests/ee/test_journal_dep.py — Coverage for the shared ``get_journal``
# FastAPI dependency shipped in feat/ee-journal-dep.
# Created: 2026-04-16 — Pins three contracts the rest of ee/ depends on:
# the dep returns a real ``Journal`` instance, successive calls hit the
# cache (one Journal per process), and ``SOUL_DATA_DIR`` is honored as
# the override knob operators use to pin the data dir to a custom volume.

from __future__ import annotations

from pathlib import Path

import pytest
from soul_protocol.engine.journal import Journal

from ee.journal_dep import _org_data_dir, get_journal, reset_journal_cache


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Each test gets a disposable ``SOUL_DATA_DIR`` + a clean cache.

    The lru_cache is module-global, so without the reset a stale instance
    from a previous test would mask env-var changes in the next one.
    """

    monkeypatch.setenv("SOUL_DATA_DIR", str(tmp_path))
    reset_journal_cache()
    yield
    reset_journal_cache()


class TestGetJournal:
    def test_returns_journal_instance(self) -> None:
        """The dep returns a ready-to-use ``Journal`` rooted at the org
        data dir. Callers should be able to ``append()`` immediately.
        """

        journal = get_journal()
        assert isinstance(journal, Journal)

    def test_is_cached_across_calls(self) -> None:
        """Two calls inside one process return the exact same instance —
        re-opening SQLite on every request would churn file handles and
        defeat the point of the dependency.
        """

        first = get_journal()
        second = get_journal()
        assert first is second

    def test_honors_soul_data_dir_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """``SOUL_DATA_DIR`` overrides the default ``~/.soul/`` location
        so operators can point an install at any volume without editing
        code. The override must be live — not frozen at import time.
        """

        custom = tmp_path / "custom-soul-data"
        monkeypatch.setenv("SOUL_DATA_DIR", str(custom))
        reset_journal_cache()

        resolved = _org_data_dir()
        assert resolved == custom

        # Opening the journal creates the dir + the sqlite file, proving
        # the env var flowed all the way through.
        journal = get_journal()
        assert isinstance(journal, Journal)
        assert (custom / "journal.db").exists()


class TestResetJournalCache:
    def test_drops_cached_instance(self) -> None:
        """``reset_journal_cache()`` is the escape hatch for tests that
        need a fresh Journal. After reset the next ``get_journal()``
        call must return a new instance, not the stale one.
        """

        first = get_journal()
        reset_journal_cache()
        second = get_journal()
        assert first is not second
