# ee/journal_dep.py — Org-level Journal FastAPI dependency for ee/ routers.
# Created: 2026-04-16 (feat/ee-journal-dep) — #948's fleet router opened a
# second SQLite journal at ~/.pocketpaw/journal/fleet.db so it could emit
# the correlated install trio without a wired org journal. That works but
# splits the audit trail across two files. This module is the shared
# dependency every ee/ route should use instead: one Journal per process,
# rooted at the canonical org data dir (SOUL_DATA_DIR or ~/.soul/), so the
# whole org shares one append-only event log.
#
# SQLite WAL is concurrent-safe at the file level, but re-opening on every
# request still pays the connection + pragma cost. ``@lru_cache`` keeps one
# Python instance alive for the life of the process. Tests that need a
# disposable journal should use FastAPI's ``app.dependency_overrides``
# pattern instead of mutating the cache — ``reset_journal_cache()`` is
# offered only as a belt-and-braces escape hatch for unit-level coverage.

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from soul_protocol.engine.journal import Journal, open_journal


def _org_data_dir() -> Path:
    """Resolve the canonical org data directory.

    ``SOUL_DATA_DIR`` wins when set — that's how operators point an
    install at a custom volume. Falls back to ``~/.soul/`` which matches
    the default soul-protocol engine layout.
    """

    env = os.environ.get("SOUL_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".soul"


@lru_cache(maxsize=1)
def _cached_journal() -> Journal:
    """Open the org journal once per process and reuse it thereafter."""

    data_dir = _org_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return open_journal(data_dir / "journal.db")


def get_journal() -> Journal:
    """FastAPI dependency returning the org's canonical Journal.

    Pair with ``Depends(get_journal)`` in route signatures. Tests should
    override via ``app.dependency_overrides[get_journal] = ...`` rather
    than touching ``_cached_journal`` directly.
    """

    return _cached_journal()


def reset_journal_cache() -> None:
    """Drop the cached Journal instance — for tests that need isolation."""

    _cached_journal.cache_clear()
