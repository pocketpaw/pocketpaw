---
{
  "title": "MongoDB Diagnostic: List All Databases and Collection Document Counts",
  "summary": "A diagnostic script that connects to a local MongoDB instance and prints every non-system database alongside its collections and document counts. It exists to quickly answer the question \"where did my data actually land?\" when debugging unexpected data routing between the local dev database, the enterprise cloud database, and any other MongoDB instance on the machine.",
  "concepts": [
    "MongoDB",
    "Motor",
    "AsyncIOMotorClient",
    "database diagnostics",
    "collection counts",
    "data routing",
    "local dev",
    "paw-cloud",
    "asyncio",
    "developer tooling"
  ],
  "categories": [
    "scripts",
    "diagnostics",
    "database",
    "developer-tools"
  ],
  "source_docs": [
    "6dd3dcd732c9f159"
  ],
  "backlinks": null,
  "word_count": 388,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`scripts/diag_mongo_all.py` is a single-purpose developer utility. Its entire purpose is captured in its module docstring: "List all databases + their collections to find where data actually landed." This framing reveals the problem it solves: PocketPaw has multiple MongoDB backends (local dev, cloud enterprise), and data can be routed to any of them depending on environment variables and config. When a feature appears to not be persisting data, the first diagnostic question is "which database and collection is receiving writes?"

## Why This Exists

During development of the enterprise cloud memory backend, the team encountered situations where data was being written to an unexpected database — for example, the default `pocketpaw` database instead of `paw-cloud`, or a test database that was never cleaned up. Rather than repeatedly opening MongoDB Compass or running `mongosh` commands from memory, this script provides a repeatable, zero-argument diagnostic.

## What It Does

The script connects to `mongodb://localhost:27017`, lists all databases, and for each non-system database (skipping `admin`, `config`, and `local`) prints the collection names and document counts:

```
Databases on localhost:27017: 5

paw-cloud  (42 docs across 6 cols)
  sessions=12, messages=28, users=2

pocketpaw  (0 docs across 3 cols)
```

Collections with zero documents are omitted from the per-database output — this keeps the output focused on where data actually is, rather than listing empty scaffolding.

## Design Choices

`motor.motor_asyncio.AsyncIOMotorClient` is imported inside `main()` rather than at module level. This allows the script to produce a useful error message if `motor` is not installed, and avoids import errors on machines where the package is not in the active Python environment.

The system databases (`admin`, `config`, `local`) are explicitly skipped. These exist on every MongoDB instance and contain internal metadata; including them in the output would add noise without value.

## Usage

```bash
uv run python scripts/diag_mongo_all.py
```

No arguments, no environment variables required. It always connects to `localhost:27017`.

## Known Gaps

- The script only connects to `localhost:27017`; it cannot inspect remote or Atlas-hosted MongoDB instances without code modification.
- Document counts use `count_documents({})` which performs a collection scan — on large collections this could be slow. For diagnostic use this is acceptable, but the script should not be run against production databases.
- There is no filtering by database name pattern; if the machine has many development databases, the output can be long.