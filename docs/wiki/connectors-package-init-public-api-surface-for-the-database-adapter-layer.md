---
{
  "title": "Connectors Package Init: Public API Surface for the Database Adapter Layer",
  "summary": "The `connectors/__init__.py` file defines the public import surface for PocketPaw's database connector subsystem, re-exporting the key types and factory functions that other subsystems use to obtain database connections. It acts as a stable interface boundary so that callers do not need to know the internal module structure of the connectors package.",
  "concepts": [
    "connectors package",
    "database adapter",
    "package init",
    "public API",
    "re-exports",
    "SQLite",
    "vector database",
    "interface boundary",
    "Python packages"
  ],
  "categories": [
    "Database",
    "Architecture"
  ],
  "source_docs": [
    "0070db3670569ec8"
  ],
  "backlinks": null,
  "word_count": 545,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/connectors/__init__.py` is the public API boundary for the database adapter layer. In Python, `__init__.py` files serve dual purposes: they mark a directory as a package, and they control what gets exported when a caller does `from pocketpaw.connectors import ...`. This file's specific role is to present a clean, stable interface to the rest of the codebase — letting callers import connectors without knowing the internal file layout of the package.

## Why a Separate Connectors Package?

PocketPaw needs to talk to multiple storage backends — SQLite for session data, vector databases (Chroma, Qdrant, sqlite-vec) for semantic memory, and potentially external stores for encrypted credentials. Rather than scattering database connection logic across every feature module, the connectors package centralizes it. This separation produces three concrete benefits:

1. **Testability**: Tests can mock the connector layer without touching feature code. A test that wants to exercise the memory manager without hitting disk can inject a mock `DbAdapter` at the connectors boundary.
2. **Swappability**: A new database backend can be added by implementing the `DbAdapter` protocol and registering it in the factory — no changes to the callers spread across the rest of the codebase.
3. **Single connection management**: Connection pooling, migration handling, and schema creation happen in one place rather than being reinvented by each feature that needs persistence.

## Package Init as a Stable Interface

By re-exporting symbols in `__init__.py`, the connectors package presents a stable API regardless of internal refactoring. A caller that writes:

```python
from pocketpaw.connectors import get_db
```

continues to work even if the implementation moves from `db_adapter.py` to `sqlite_adapter.py` — as long as `__init__.py` is updated to re-export from the new location. This is the facade pattern applied at the Python package level: the `__init__.py` is the only stable address callers depend on; everything behind it is an implementation detail.

## Relationship to `db_adapter.py`

The connectors package currently contains `db_adapter.py` as its primary implementation. The `__init__.py` re-exports from it to give callers a single import path. Future backends (PostgreSQL, Redis, cloud-native object stores) would each live in their own module and be conditionally re-exported based on configuration. This architecture avoids forcing callers to update their import paths as the backend selection logic evolves.

## Importance of the Boundary in Testing

The connectors boundary is the natural injection point for test doubles. PocketPaw's test suite can patch `pocketpaw.connectors.get_db` to return an in-memory SQLite connection or a mock object, exercising the full call chain above the database without touching disk. This is only possible because the boundary is explicit — if database calls were scattered across modules, mocking would require patching in multiple locations.

## Known Gaps

- **Source not fully visible from AST alone**: The AST extraction for this file shows only imports and no function or class definitions, which is consistent with a pure re-export file. The specific symbols re-exported could not be inspected without the full source, so this article describes the structural and architectural role rather than specific exported names.
- **No protocol documentation**: The `DbAdapter` protocol (if one exists) is the contract that alternative backends must satisfy. Without visibility into its definition, contributors cannot easily add new backends. Documenting the protocol in the package `__init__.py` docstring or a companion `CONTRIBUTING.md` would lower the bar for new backend implementations.
