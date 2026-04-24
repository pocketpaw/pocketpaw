---
{
  "title": "get_journal FastAPI Dependency: Caching, SOUL_DATA_DIR Override, and Cache Reset Tests",
  "summary": "Tests the `get_journal` FastAPI dependency from `ee.journal_dep`, pinning three contracts that the rest of the `ee/` subsystem depends on: the dep returns a live Journal instance, repeated calls within one process return the same cached instance, and the `SOUL_DATA_DIR` environment variable is honoured at call time — not frozen at import. Also verifies `reset_journal_cache()` as the test-isolation escape hatch.",
  "concepts": [
    "get_journal",
    "FastAPI dependency",
    "lru_cache",
    "SOUL_DATA_DIR",
    "reset_journal_cache",
    "Journal",
    "soul_protocol",
    "test isolation",
    "env var override",
    "SQLite"
  ],
  "categories": [
    "testing",
    "dependency injection",
    "journal",
    "configuration",
    "test"
  ],
  "source_docs": [
    "f95c737e912acf12"
  ],
  "backlinks": null,
  "word_count": 472,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee.journal_dep.get_journal` is the single shared FastAPI dependency used across every `ee/` router that needs to read or write journal events. It wraps `soul_protocol.engine.journal.Journal` behind an `lru_cache`, so the first call opens a SQLite connection and all subsequent calls within the same process reuse it. `tests/ee/test_journal_dep.py` was created in `feat/ee-journal-dep` to lock down that behaviour before the rest of Wave 3 built on top of it.

## Why Each Contract Matters

### Returns a Journal Instance
Callers invoke `get_journal()` and immediately call `.append()` or `.query()` on the result. If the dep returned something other than a real `Journal` — say, a partially-initialised proxy — those calls would fail at runtime with obscure attribute errors rather than a clean test failure. The `isinstance(journal, Journal)` assertion makes the contract explicit.

### Caching Across Calls
SQLite connections are not free. If `get_journal` opened a new connection on every FastAPI request, a busy endpoint would exhaust file descriptors and degrade performance. The `test_is_cached_across_calls` test asserts `first is second` (identity, not equality) to confirm the `lru_cache` is working, not just that two journals happen to point at the same file.

### SOUL_DATA_DIR Honour at Call Time

```python
custom = tmp_path / "custom-soul-data"
monkeypatch.setenv("SOUL_DATA_DIR", str(custom))
reset_journal_cache()
resolved = _org_data_dir()
assert resolved == custom
```

Operators deploying PocketPaw on a server with a custom data volume set `SOUL_DATA_DIR` to redirect the journal away from `~/.soul/`. The test verifies two things: the env var is read dynamically (not captured at import time), and opening the journal creates the directory + SQLite file at the custom path.

### reset_journal_cache as Escape Hatch
Because the cache is module-global, tests that change `SOUL_DATA_DIR` mid-run would otherwise see the stale instance from the first call. `reset_journal_cache()` drops the cached reference so the next `get_journal()` re-evaluates the env var. The autouse `_isolate_cache` fixture calls it before and after every test:

```python
@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("SOUL_DATA_DIR", str(tmp_path))
    reset_journal_cache()
    yield
    reset_journal_cache()
```

This pattern — set env, reset cache, yield, reset cache — appears in every `ee/` test file that touches the journal. The shared pattern ensures that a test which forgets to call `reset_journal_cache` does not corrupt later tests in the same session.

## Failure Modes Prevented

- **Stale handle leak**: Without `reset_journal_cache`, a test that writes to `tmp_path/A` would leave a cached handle. The next test, pointing at `tmp_path/B`, would still read from A, producing false positives on existence checks and phantom query results.
- **Import-time env capture**: If `_org_data_dir` read `SOUL_DATA_DIR` at import rather than at call time, no operator-level override would work in production without restarting the process.

## Known Gaps

There is no test for concurrent access (two asyncio tasks calling `get_journal()` simultaneously during cache warm-up). Python's GIL makes a race unlikely in CPython, but the test gap is worth noting for any future move to a multi-process deployment model.