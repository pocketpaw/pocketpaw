# ee/api.py — Singleton entry point for the Instinct decision pipeline store.
# Created: 2026-03-30 — Bridges instinct_tools.py to the InstinctStore.
# The agent tools (pocketpaw.tools.builtin.instinct_tools) import from here
# via `from ee.api import get_instinct_store`.

from __future__ import annotations

from pathlib import Path

from ee.instinct.store import InstinctStore

_DB_PATH = Path.home() / ".pocketpaw" / "instinct.db"

_store: InstinctStore | None = None


def get_instinct_store() -> InstinctStore:
    """Return the global InstinctStore singleton.

    Lazily creates the store on first call. The SQLite database is stored
    at ~/.pocketpaw/instinct.db (same as the router uses).
    """
    global _store
    if _store is None:
        _store = InstinctStore(_DB_PATH)
    return _store
