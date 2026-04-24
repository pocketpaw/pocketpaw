---
{
  "title": "Files Test Fixtures: FakeProvider, ctx, make_entry, make_mount",
  "summary": "This conftest.py provides the shared test infrastructure for the files subsystem: a FakeProvider that implements the full provider protocol in memory, plus factory fixtures for RequestContext, FileEntry, and ResolvedMount. All file provider tests build on these primitives.",
  "concepts": [
    "FakeProvider",
    "provider protocol",
    "RequestContext",
    "FileEntry",
    "ResolvedMount",
    "ProviderUnsupported",
    "conftest.py",
    "fixture factory",
    "baseline_rbac",
    "open_stream",
    "AsyncIterator"
  ],
  "categories": [
    "testing",
    "fixtures",
    "files",
    "provider abstraction",
    "test"
  ],
  "source_docs": [
    "tests/cloud/files/conftest.py"
  ],
  "backlinks": null,
  "word_count": 440,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/files/conftest.py` defines the shared fixtures and the `FakeProvider` class used across the files test suite. Its role is to provide a fully compliant, in-memory implementation of the file provider protocol so individual tests can control exactly what data the provider returns without hitting a real storage backend.

## FakeProvider

```python
class FakeProvider:
    def __init__(self, provider_id, mounts=None, entries=None):
        self.provider_id = provider_id
        self._mounts = mounts or []
        self._entries = entries or []
```

`FakeProvider` implements every method in the provider protocol:
- `list_mounts(ctx)` — returns the configured mounts list
- `list_entries(ctx, mount_path, cursor, limit, filters)` — filters entries by mount path prefix
- `get_entry(ctx, entry_id)` — looks up by ID, raises `ProviderUnsupported` (or `KeyError`) if not found
- `open_stream(ctx, entry_id)` — returns an async generator yielding bytes
- `upload`, `rename`, `move`, `delete`, `search` — raise `ProviderUnsupported` to signal the provider does not support write operations

The `ProviderUnsupported` raises for write operations are intentional: they allow the provider contract tests to assert that unsupported operations produce the correct exception type rather than a generic error.

### _make Helpers

`FakeProvider` includes two private factory methods:
- `_make(provider_id, native_id, mount, **overrides)` → `FileEntry` — constructs a `FileEntry` with sensible defaults, allowing tests to override only the fields they care about
- `_make(provider_id, path, writable, order)` → `ResolvedMount` — constructs a `ResolvedMount`

### baseline_rbac

The `baseline_rbac(ctx, entry)` method returns a `Permission` value based on the entry and the request context. This is used by the provider contract tests to verify that the provider correctly computes permissions without involving the ABAC layer.

## Shared Fixtures

### ctx
```python
@pytest.fixture
def ctx() -> RequestContext:
    return RequestContext(user_id="u1", workspace_id="ws_1", attributes={})
```

The `ctx` fixture provides a standard `RequestContext` for tests that do not need to vary the user or workspace. Tests that need a different context construct it inline.

### make_entry and make_mount
These fixtures return the `FakeProvider._make` factory functions, allowing tests to create `FileEntry` and `ResolvedMount` instances with minimal boilerplate:

```python
def test_something(make_entry):
    entry = make_entry("uploads", "native-id-1", "/Uploads/ws_1")
```

## Why a Shared FakeProvider

Without `FakeProvider`, each test file would need its own stub implementation. This leads to drift: one stub implements `list_mounts` differently from another, making it unclear whether a test failure is due to the production code or the stub. A shared, fully-compliant implementation used everywhere means any compliance failure in the stub affects all tests equally and is caught immediately.

## Known Gaps

No TODOs or FIXMEs are present. The `list_entries` implementation filters only by mount path prefix — it does not respect the `cursor`, `limit`, or `filters` parameters. Tests that need pagination or filtering behavior must construct a provider with pre-filtered entry lists.
