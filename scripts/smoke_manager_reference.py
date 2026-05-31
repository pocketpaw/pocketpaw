"""Smoke test — MemoryManager singleton is swapped in place on ee bootstrap.

Reproduces the real bug: AgentLoop captures `self.memory = get_memory_manager()`
at construction time, which happens BEFORE init_cloud_db runs. When bootstrap
flips the backend, the already-held reference must see the new store.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid


def _banner(t: str) -> None:
    print(f"\n=== {t} ===")


async def main() -> int:
    db_name = f"smoke_manager_ref_{uuid.uuid4().hex[:8]}"
    uri = f"mongodb://localhost:27017/{db_name}"

    os.environ["POCKETPAW_CLOUD_MONGO_URI"] = uri
    os.environ.pop("POCKETPAW_MEMORY_BACKEND", None)

    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    from pocketpaw.memory.file_store import FileMemoryStore
    from pocketpaw.memory.manager import get_memory_manager

    _banner("1. get_memory_manager() BEFORE init_cloud_db — expect FileMemoryStore")
    manager = get_memory_manager()
    assert isinstance(manager._store, FileMemoryStore), (
        f"expected FileMemoryStore, got {type(manager._store).__name__}"
    )
    print(f"   manager._store = {type(manager._store).__name__}")

    # Simulate agent_loop caching its memory reference.
    agent_cached_memory = manager
    print(f"   agent_cached_memory is manager: {agent_cached_memory is manager}")

    _banner("2. init_beanie + register_default_backend (simulates startup)")
    AsyncIOMotorClient("mongodb://localhost:27017")
    await init_beanie(
        connection_string=uri,
        document_models=[*ALL_DOCUMENTS, MemoryFactDoc],
    )

    from pocketpaw_ee.cloud.memory.bootstrap import register_default_backend

    register_default_backend()

    _banner("3. cached reference must now see MongoMemoryStore")
    print(f"   agent_cached_memory._store = {type(agent_cached_memory._store).__name__}")
    if not isinstance(agent_cached_memory._store, MongoMemoryStore):
        print("\nSMOKE FAILED: cached MemoryManager still on old store")
        return 3

    _banner("4. write via cached reference, read from Mongo")
    key = f"smoke-{uuid.uuid4().hex[:8]}"
    await agent_cached_memory.add_to_session(key, "user", "agent-loop path works")

    from pocketpaw_ee.cloud.models.message import Message

    rows = await Message.find({"session_key": key}).to_list()
    print(f"   {len(rows)} messages with session_key={key!r}")
    assert len(rows) == 1 and rows[0].context_type == "pocket"
    assert rows[0].content == "agent-loop path works"

    print("\nSMOKE OK")

    # Cleanup
    client2 = AsyncIOMotorClient("mongodb://localhost:27017")
    await client2.drop_database(db_name)
    print(f"(dropped {db_name})")
    # Reset the global so other scripts start clean
    import pocketpaw.memory.manager as _mm

    _mm._manager = None
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
