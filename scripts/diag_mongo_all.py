"""List all databases + their collections to find where data actually landed."""

from __future__ import annotations

import asyncio


async def main() -> int:
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient("mongodb://localhost:27017")
    dbs = await client.list_database_names()
    print(f"Databases on localhost:27017: {len(dbs)}")
    for db_name in sorted(dbs):
        if db_name in ("admin", "config", "local"):
            continue
        db = client[db_name]
        cols = await db.list_collection_names()
        total = 0
        per_col = []
        for c in cols:
            n = await db[c].count_documents({})
            total += n
            if n:
                per_col.append(f"{c}={n}")
        if total:
            print(f"\n{db_name}  ({total} docs across {len(cols)} cols)")
            print("  " + ", ".join(per_col))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
