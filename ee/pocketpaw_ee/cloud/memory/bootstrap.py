"""ee memory backend bootstrap.

Flips the default memory backend to ``mongodb`` for ee cloud deployments while
respecting any explicit ``POCKETPAW_MEMORY_BACKEND`` override. Called from
``init_cloud_db`` before Beanie is initialised.

The flip bypasses ``Settings.load()`` (which reads ``~/.pocketpaw/config.json``
and would keep an older ``memory_backend: "file"`` value). Instead it primes
the ``pocketpaw.memory.manager`` singleton directly with a ``MongoMemoryStore``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def register_default_backend() -> None:
    """Default to ``mongodb`` when no explicit backend is configured.

    No-op when ``POCKETPAW_MEMORY_BACKEND`` is set to anything other than
    ``"mongodb"`` — user config wins. When unset (or set to ``"mongodb"``),
    primes the memory manager singleton with a ``MongoMemoryStore`` so the
    next ``get_memory_manager()`` call uses Mongo regardless of JSON config.
    """
    explicit = os.environ.get("POCKETPAW_MEMORY_BACKEND")
    if explicit and explicit != "mongodb":
        logger.info("ee: POCKETPAW_MEMORY_BACKEND=%r set by user, not overriding", explicit)
        return

    os.environ["POCKETPAW_MEMORY_BACKEND"] = "mongodb"

    # Flush cached config so any caller reading Settings sees the new backend.
    try:
        from pocketpaw.config import get_settings  # type: ignore[import-untyped]

        get_settings.cache_clear()
    except Exception:  # noqa: BLE001
        logger.debug("ee: failed to clear settings cache", exc_info=True)

    # Install MongoMemoryStore into the manager singleton.
    #
    # Critical: if the singleton already exists (e.g. `AgentLoop()` was
    # constructed at module-import time and called `get_memory_manager()`
    # before init_cloud_db ran), we must **swap ._store in place** instead
    # of replacing `_mm._manager`. Any cached `manager` reference held by
    # `agent_loop.memory` keeps working and automatically picks up MongoDB.
    # If we rebind `_mm._manager` to a fresh
    # instance, those cached references stay bound to the old FileMemoryStore
    # and silently write to disk instead of Mongo.
    try:
        import pocketpaw.memory.manager as _mm  # type: ignore[import-untyped]
        from pocketpaw.memory.manager import MemoryManager  # type: ignore[import-untyped]
        from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

        store = MongoMemoryStore()
        if _mm._manager is None:
            _mm._manager = MemoryManager(store=store)
        else:
            _mm._manager._store = store
    except Exception:  # noqa: BLE001
        logger.exception("ee: failed to prime MongoMemoryStore manager")
        return

    logger.info("ee: memory backend set to 'mongodb'")
