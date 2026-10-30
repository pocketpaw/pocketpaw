"""Fail-fast guard: the cloud must run on MongoMemoryStore, never local disk.

Reproduces the "files chats stored locally" failure mode: if the active memory
backend is the local FileMemoryStore in a cloud process, chat history (files
surface included) is written to ``~/.pocketpaw/memory/sessions/*.json`` instead
of Mongo. ``verify_cloud_memory_backend`` turns that misconfiguration into a
hard boot failure; ``register_default_backend`` installs Mongo and raises if it
can't. These tests pin both behaviours.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.memory.bootstrap import (
    register_default_backend,
    verify_cloud_memory_backend,
)
from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

pytestmark = pytest.mark.usefixtures("mongo_db")


@pytest.fixture(autouse=True)
def _restore_backend(monkeypatch: pytest.MonkeyPatch):
    """Isolate the global memory-manager singleton + backend env per test."""
    import pocketpaw.memory.manager as mm

    monkeypatch.delenv("POCKETPAW_MEMORY_BACKEND", raising=False)
    saved = mm._manager
    yield
    mm._manager = saved


def test_register_default_backend_installs_mongo() -> None:
    """After the bootstrap flip, the active store is a MongoMemoryStore."""
    from pocketpaw.memory.manager import get_memory_manager

    register_default_backend()
    assert isinstance(get_memory_manager()._store, MongoMemoryStore)


def test_verify_passes_when_mongo_active() -> None:
    register_default_backend()
    # Should not raise.
    verify_cloud_memory_backend()


def test_verify_raises_when_file_store_active(tmp_path) -> None:
    """A FileMemoryStore in the cloud must fail the boot, not write to disk."""
    import pocketpaw.memory.manager as mm
    from pocketpaw.memory.file_store import FileMemoryStore

    register_default_backend()
    # Simulate a stale / overridden backend: swap a local store back in.
    mm._manager._store = FileMemoryStore(base_path=tmp_path)

    with pytest.raises(RuntimeError, match="MongoMemoryStore"):
        verify_cloud_memory_backend()
