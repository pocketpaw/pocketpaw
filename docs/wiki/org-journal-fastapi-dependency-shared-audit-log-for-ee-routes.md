---
{
  "title": "Org Journal FastAPI Dependency — Shared Audit Log for EE Routes",
  "summary": "Provides a single shared `Journal` instance as a FastAPI dependency, ensuring all enterprise edition routes write their audit events to one canonical org-level log rather than opening separate per-subsystem SQLite files. Supports test isolation via a cache-reset escape hatch.",
  "concepts": [
    "get_journal",
    "FastAPI dependency",
    "lru_cache",
    "org journal",
    "SOUL_DATA_DIR",
    "audit trail consolidation",
    "WAL mode",
    "dependency_overrides",
    "reset_journal_cache",
    "Journal",
    "soul-protocol engine"
  ],
  "categories": [
    "audit and compliance",
    "FastAPI",
    "enterprise edition",
    "data persistence"
  ],
  "source_docs": [
    "50083efa848e2882"
  ],
  "backlinks": null,
  "word_count": 462,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Problem Solved

Before this module existed, the fleet router opened its own SQLite journal at `~/.pocketpaw/journal/fleet.db` to emit correlated install events. This worked functionally but created a split audit trail: fleet events lived in one file while every other EE subsystem wrote to the soul-protocol engine journal. An org admin querying the audit log would miss fleet events entirely. `journal_dep.py` eliminates this fragmentation.

## _org_data_dir

Resolves the canonical org data directory using a two-step lookup:
1. `SOUL_DATA_DIR` environment variable — operators running on custom volumes or containerized deployments set this to point at the mounted data directory.
2. Falls back to `~/.soul/` — the default soul-protocol engine layout.

This matches the directory convention used by soul-protocol's engine so the journal lands in the same directory as the soul database files, keeping the entire org state co-located.

## _cached_journal and @lru_cache

`_cached_journal()` is decorated with `@lru_cache(maxsize=1)`. This means the `Journal` is opened once per Python process and the same instance is returned on every subsequent call. The rationale is SQLite-specific: even though SQLite WAL mode is safe for concurrent readers and writers at the file level, re-opening the database on every HTTP request pays the connection setup cost and the WAL pragma application on each open. A cached instance amortizes that cost across thousands of requests.

## get_journal — The Public FastAPI Dependency

`get_journal()` is the function used in FastAPI route signatures:

```python
@router.post("/fleet/install")
async def install(req: InstallFleetRequest, journal: Journal = Depends(get_journal)):
    ...
```

It simply calls `_cached_journal()`. The thin wrapper exists so the dependency injection framework sees a callable it can resolve, and so the public name is decoupled from the caching implementation.

## reset_journal_cache — Test Escape Hatch

`reset_journal_cache()` calls `_cached_journal.cache_clear()` to drop the cached instance. This exists for unit tests that need a fresh, isolated journal pointed at a temporary directory. The preferred approach for FastAPI tests is `app.dependency_overrides[get_journal] = lambda: my_test_journal`, which does not touch the module-level cache. `reset_journal_cache()` is described in the source comments as a "belt-and-braces escape hatch" for cases where dependency override is not available.

## Why One Journal Matters

A single org journal enables:
- Cross-subsystem event correlation: install events, instinct approvals, and retrieval queries all share a timeline.
- A single SQL query for compliance exports rather than joining across files.
- Consistent `SOUL_DATA_DIR`-based backup — one directory covers the entire audit trail.

## Known Gaps

- `@lru_cache` is process-local; in multi-worker deployments (e.g., Gunicorn with 4 workers), each worker has its own cached `Journal` instance pointing at the same SQLite file. WAL mode handles the file-level concurrency, but Python-level state (if any) is not shared.
- No health check exposes whether the cached journal is healthy; a corrupted SQLite file will surface as an error on the first write, not at startup.