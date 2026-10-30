"""Smoke test — ee MongoDB memory backend end-to-end.

Exercises the full path the way a running ee server would:
1. init_cloud_db(...) flips the memory backend default to ``mongodb`` and
   primes the memory manager singleton with a MongoMemoryStore.
2. MemoryManager.add_to_session(...) writes a pocket-context row.
3. MemoryManager.get_session_history(...) reads it back in LLM message format.
4. Raw Mongo inspection confirms the row landed in the ``messages`` collection
   with context_type=="pocket", session_key set, and the expected content.
5. A LONG_TERM entry writes to ``memory_facts``.

Usage:
    uv run python scripts/smoke_mongo_memory.py

Expects a MongoDB instance at localhost:27017 (override with
POCKETPAW_SMOKE_MONGO_URI). The test database is dropped at the end.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid


def _banner(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> int:
    # Pick a throwaway database so we can safely drop it at the end.
    db_name = f"smoke_mongo_memory_{uuid.uuid4().hex[:8]}"
    mongo_uri = os.environ.get("POCKETPAW_SMOKE_MONGO_URI", f"mongodb://localhost:27017/{db_name}")
    # Make sure no prior env selection overrides the ee default.
    os.environ.pop("POCKETPAW_MEMORY_BACKEND", None)

    _banner("1. init_cloud_db flips backend")
    from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db

    await init_cloud_db(mongo_uri)
    assert os.environ["POCKETPAW_MEMORY_BACKEND"] == "mongodb", "env not flipped"
    print("   POCKETPAW_MEMORY_BACKEND =", os.environ["POCKETPAW_MEMORY_BACKEND"])

    _banner("2. memory manager is MongoMemoryStore")
    from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

    from pocketpaw.memory.manager import get_memory_manager

    manager = get_memory_manager()
    assert isinstance(manager._store, MongoMemoryStore), (
        f"expected MongoMemoryStore, got {type(manager._store).__name__}"
    )
    print("   manager._store =", type(manager._store).__name__)

    _banner("3. add_to_session writes a pocket row")
    session_key = f"smoke-{uuid.uuid4().hex[:8]}"
    user_msg_id = await manager.add_to_session(session_key, "user", "Hello, PocketPaw!")
    agent_msg_id = await manager.add_to_session(session_key, "assistant", "Hi — how can I help?")
    print(f"   user_msg_id = {user_msg_id}")
    print(f"   agent_msg_id = {agent_msg_id}")
    assert len(user_msg_id) == 24 and len(agent_msg_id) == 24, "expected 24-char hex ids"

    _banner("4. get_session_history round-trips")
    history = await manager.get_session_history(session_key)
    print(f"   history = {history}")
    assert history == [
        {"role": "user", "content": "Hello, PocketPaw!"},
        {"role": "assistant", "content": "Hi — how can I help?"},
    ], "unexpected history shape"

    _banner("5. raw Mongo inspection — messages collection")
    from pocketpaw_ee.cloud.models.message import Message

    rows = await Message.find({"session_key": session_key}).to_list()
    assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
    for r in rows:
        assert r.context_type == "pocket", f"bad context_type: {r.context_type!r}"
        assert r.session_key == session_key
        assert r.role in ("user", "assistant")
        assert not r.group, "pocket rows must not have group set"
    print(f"   {len(rows)} pocket-context rows in `messages`, all with session_key={session_key!r}")

    _banner("6. LONG_TERM entry writes to memory_facts")
    long_term_id = await manager.remember(
        "Smoke test user prefers concise replies", tags=["preferences"]
    )
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc

    fact = await MemoryFactDoc.get(long_term_id) if long_term_id else None
    assert fact is not None, "memory_facts doc not found"
    assert fact.type == "long_term"
    assert "preferences" in fact.tags
    print(f"   long_term_id = {long_term_id}, tags={fact.tags}")

    _banner("7. Teardown — drop test DB")
    from motor.motor_asyncio import AsyncIOMotorClient

    await close_cloud_db()
    client = AsyncIOMotorClient("mongodb://localhost:27017")
    await client.drop_database(db_name)
    print(f"   dropped {db_name}")

    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
