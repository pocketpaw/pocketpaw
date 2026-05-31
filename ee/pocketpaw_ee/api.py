# pocketpaw_ee/api.py — backwards-compatible re-export shim.
#
# The singleton store factories moved to pocketpaw.stores (OSS core) in the
# open-core split (Phase 3) — they are plain SQLite stores with no cloud
# dependency. The enterprise routers (instinct/router.py, paw_print/router.py)
# still import `from pocketpaw_ee.api import get_*_store`; this shim keeps
# those import paths valid. New code should import from pocketpaw.stores.

from __future__ import annotations

from pocketpaw.stores import get_fabric_store, get_instinct_store, get_paw_print_store

__all__ = ["get_fabric_store", "get_instinct_store", "get_paw_print_store"]
