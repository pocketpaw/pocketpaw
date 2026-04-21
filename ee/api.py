# ee/api.py — Singleton entry points for the ee-scoped stores.
# Created: 2026-03-30 — Bridges instinct_tools.py to the InstinctStore.
# Updated: 2026-04-20 — Added get_paw_print_store() so the paw_print router
#   can reach the SQLite-backed PawPrintStore from a non-DI import path.
# The agent tools (pocketpaw.tools.builtin.instinct_tools) import from here
# via `from ee.api import get_instinct_store`; paw_print router uses
# `from ee.api import get_paw_print_store`.

from __future__ import annotations

from pathlib import Path

from ee.instinct.store import InstinctStore
from ee.paw_print.store import PawPrintStore

_DB_PATH = Path.home() / ".pocketpaw" / "instinct.db"
_PAW_PRINT_DB_PATH = Path.home() / ".pocketpaw" / "paw_print.db"

_store: InstinctStore | None = None
_paw_print_store: PawPrintStore | None = None


def get_instinct_store() -> InstinctStore:
    """Return the global InstinctStore singleton.

    Lazily creates the store on first call. The SQLite database is stored
    at ~/.pocketpaw/instinct.db (same as the router uses).
    """
    global _store
    if _store is None:
        _store = InstinctStore(_DB_PATH)
    return _store


def get_paw_print_store() -> PawPrintStore:
    """Return the global PawPrintStore singleton.

    Lazily creates the store on first call. The SQLite database lives at
    ~/.pocketpaw/paw_print.db. Referenced by `ee/paw_print/router.py` via
    its `_store()` helper so the widget CRUD and event ingest endpoints
    can reach the shared instance.
    """
    global _paw_print_store
    if _paw_print_store is None:
        _paw_print_store = PawPrintStore(_PAW_PRINT_DB_PATH)
    return _paw_print_store
