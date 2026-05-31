"""Process-wide singletons for the local SQLite-backed runtime stores.

Instinct, Fabric and Paw Print all keep their state in SQLite under
``~/.pocketpaw/``. These factories return a lazily-created singleton each so
agent tools, automations and routers share one instance per process.

This is plain core infrastructure — there is no cloud-backed override. The
factories moved here from ``pocketpaw_ee/api.py`` in the OSS-EE split
(Phase 3); ``pocketpaw_ee.api`` now re-exports from this module for the
enterprise routers that still import via that path.
"""

from __future__ import annotations

from pathlib import Path

from pocketpaw.fabric.store import FabricStore
from pocketpaw.instinct.store import InstinctStore
from pocketpaw.paw_print.store import PawPrintStore

_DATA_DIR = Path.home() / ".pocketpaw"

_instinct_store: InstinctStore | None = None
_fabric_store: FabricStore | None = None
_paw_print_store: PawPrintStore | None = None


def get_instinct_store() -> InstinctStore:
    """Return the global InstinctStore singleton (``~/.pocketpaw/instinct.db``)."""
    global _instinct_store
    if _instinct_store is None:
        _instinct_store = InstinctStore(_DATA_DIR / "instinct.db")
    return _instinct_store


def get_fabric_store() -> FabricStore:
    """Return the global FabricStore singleton (``~/.pocketpaw/fabric.db``)."""
    global _fabric_store
    if _fabric_store is None:
        _fabric_store = FabricStore(_DATA_DIR / "fabric.db")
    return _fabric_store


def get_paw_print_store() -> PawPrintStore:
    """Return the global PawPrintStore singleton (``~/.pocketpaw/paw_print.db``)."""
    global _paw_print_store
    if _paw_print_store is None:
        _paw_print_store = PawPrintStore(_DATA_DIR / "paw_print.db")
    return _paw_print_store
