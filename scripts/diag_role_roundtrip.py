"""Diagnose: agent-loop saves both user and assistant — verify roles round-trip."""

from __future__ import annotations

import asyncio
import os
import sys
import uuid


async def main() -> int:
    db_name = f"diag_role_{uuid.uuid4().hex[:8]}"
    uri = f"mongodb://localhost:27017/{db_name}"
    os.environ["POCKETPAW_CLOUD_MONGO_URI"] = uri
    os.environ.pop("POCKETPAW_MEMORY_BACKEND", None)

    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient
    from pocketpaw_ee.cloud.memory.bootstrap import register_default_backend
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    await init_beanie(
        connection_string=uri,
        document_models=[*ALL_DOCUMENTS, MemoryFactDoc],
    )
    register_default_backend()

    from pocketpaw.memory.manager import get_memory_manager

    manager = get_memory_manager()
    print(f"manager._store = {type(manager._store).__name__}")

    chat_id = uuid.uuid4().hex[:12]
    bus_key = f"websocket:{chat_id}"
    ui_key = f"websocket_{chat_id}"

    print(f"\nchat_id     = {chat_id}")
    print(f"bus_key     = {bus_key}")
    print(f"ui_key      = {ui_key}")

    print("\n--- writing 4 entries ---")
    await manager.add_to_session(bus_key, "user", "hi user 1")
    await manager.add_to_session(bus_key, "assistant", "hi assistant 1")
    await manager.add_to_session(bus_key, "user", "hi user 2")
    await manager.add_to_session(bus_key, "assistant", "hi assistant 2")

    print("\n--- raw Mongo dump ---")
    from pocketpaw_ee.cloud.models.message import Message

    rows = await Message.find({"session_key": ui_key}).sort("createdAt").to_list()
    for m in rows:
        print(f"  id={m.id} role={m.role!r} sender_type={m.sender_type!r} content={m.content!r}")

    print(f"\n{len(rows)} rows. Roles seen: {[m.role for m in rows]}")

    expected_roles = ["user", "assistant", "user", "assistant"]
    actual_roles = [m.role for m in rows]
    if actual_roles == expected_roles:
        print("ROUNDTRIP OK")
        result = 0
    else:
        print(f"ROUNDTRIP FAILED: expected {expected_roles}, got {actual_roles}")
        result = 1

    client2 = AsyncIOMotorClient("mongodb://localhost:27017")
    await client2.drop_database(db_name)
    return result


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
