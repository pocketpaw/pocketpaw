"""ee memory backend bootstrap.

Flips the default memory backend to ``mongodb`` for ee cloud deployments while
respecting any explicit ``POCKETPAW_MEMORY_BACKEND`` override. Called from
``init_cloud_db`` before Beanie is initialised.

The flip bypasses ``Settings.load()`` (which reads ``~/.pocketpaw/config.json``
and would keep an older ``memory_backend: "file"`` value). Instead it primes
the ``pocketpaw.memory.manager`` singleton directly with a ``MongoMemoryStore``.

Fail-fast (2026-06-06): ``register_default_backend`` now RAISES if it can't
install the Mongo store, and ``verify_cloud_memory_backend`` lets the cloud
startup refuse to boot when the active store isn't ``MongoMemoryStore`` (e.g. a
``POCKETPAW_MEMORY_BACKEND=file`` override). Both exist so a misconfigured cloud
never silently writes chat history — including files-surface chats — to local
disk.
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
    except Exception as exc:
        # A cloud deployment that can't install the Mongo-backed memory store
        # must fail loudly at startup rather than swallow the error and fall
        # back to the local FileMemoryStore — that fallback silently writes
        # chat history (including files-surface chats) to disk.
        logger.exception("ee: failed to prime MongoMemoryStore manager")
        raise RuntimeError(
            "ee: could not install MongoMemoryStore — refusing to start with a "
            "local memory backend that would write chat history to disk."
        ) from exc

    if not isinstance(_mm._manager._store, MongoMemoryStore):
        raise RuntimeError(
            f"ee: memory backend is {type(_mm._manager._store).__name__} after "
            "install, expected MongoMemoryStore."
        )

    logger.info("ee: memory backend set to 'mongodb'")


def verify_cloud_memory_backend() -> None:
    """Fail-fast guard: the cloud must run on ``MongoMemoryStore``.

    ``register_default_backend`` is a no-op when ``POCKETPAW_MEMORY_BACKEND`` is
    explicitly set to a non-mongodb value (user override). In a cloud
    deployment that override would route chat history to the local
    ``FileMemoryStore`` and write it to disk. Called right after
    ``register_default_backend`` at cloud startup so a misconfigured backend
    fails the boot instead of silently leaking chats to local files.
    """
    from pocketpaw.memory.manager import get_memory_manager
    from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

    store = get_memory_manager()._store
    if not isinstance(store, MongoMemoryStore):
        raise RuntimeError(
            f"cloud startup: memory backend is {type(store).__name__}, expected "
            "MongoMemoryStore. Refusing to start to avoid writing chat history to "
            "local disk. Unset POCKETPAW_MEMORY_BACKEND or set it to 'mongodb'."
        )
