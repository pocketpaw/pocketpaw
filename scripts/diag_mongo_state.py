"""Diagnose ee cloud Mongo state — what's actually in the DB right now.

Usage:
    uv run python scripts/diag_mongo_state.py
    POCKETPAW_CLOUD_MONGO_URI=mongodb://localhost:27017/paw-cloud \\
        uv run python scripts/diag_mongo_state.py
"""

from __future__ import annotations

import asyncio
import os


async def main() -> int:
    uri = os.environ.get("POCKETPAW_CLOUD_MONGO_URI", "mongodb://localhost:27017/paw-cloud")
    db_name = uri.rsplit("/", 1)[-1].split("?")[0] or "paw-cloud"
    print(f"Connecting to: {uri}")
    print(f"DB name: {db_name}")

    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(uri)
    db = client[db_name]

    # List all collections and row counts
    print("\n--- Collections ---")
    cols = await db.list_collection_names()
    for c in sorted(cols):
        n = await db[c].count_documents({})
        print(f"  {c}: {n}")

    # Peek at sessions + messages + users
    for col in ("sessions", "messages", "users", "workspaces", "groups"):
        if col not in cols:
            continue
        sample = await db[col].find({}).limit(3).to_list(length=3)
        print(f"\n--- {col} (first 3) ---")
        for doc in sample:
            doc_out = {k: v for k, v in doc.items() if k != "password"}
            print(" ", doc_out)

    print()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
