---
{
  "title": "FolderProvider Contract Test Base Class: Reusable Compliance Suite",
  "summary": "This module defines `ProviderContract`, an abstract base class that encodes the behavioral contract every `FolderProvider` implementation must satisfy, including mount listing, entry listing with pagination, namespaced IDs, and graceful `ProviderUnsupported` errors for write operations. Concrete provider tests subclass it to inherit full compliance coverage automatically.",
  "concepts": [
    "ProviderContract",
    "FolderProvider",
    "contract testing",
    "ProviderUnsupported",
    "namespaced IDs",
    "list_mounts",
    "list_entries",
    "plugin system",
    "abstract base class",
    "compliance tests"
  ],
  "categories": [
    "Cloud Files",
    "Testing",
    "Provider System",
    "Plugin Architecture",
    "test"
  ],
  "source_docs": [
    "eec18007e2a9824d"
  ],
  "backlinks": null,
  "word_count": 480,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/files/test_provider_contract.py` implements a contract test pattern for PocketPaw's pluggable file provider system. Rather than writing provider-specific tests that duplicate the same assertions, it provides `ProviderContract` -- an abstract base class with `@pytest.mark.asyncio` test methods that any concrete `FolderProvider` inherits by subclassing.

## The Plugin Contract Problem

PocketPaw's cloud files subsystem is designed to support multiple storage backends through the `FolderProvider` protocol: local uploads, Google Drive, knowledge base stores, and future providers. Each provider must expose a consistent interface so the router, tree builder, and browse logic can treat them interchangeably.

Without a shared contract test, three failure modes become possible:

1. **Silent protocol drift**: a new provider implements `list_entries` but returns a plain list instead of a `Page` object, breaking pagination downstream.
2. **ID namespace collisions**: if a provider returns entry IDs without a `provider_id:` prefix, entries from different providers can collide in the unified namespace.
3. **Missing `ProviderUnsupported` errors**: a read-only provider that raises `NotImplementedError` instead of `ProviderUnsupported` breaks the error translation layer, which only catches the canonical exception type.

`ProviderContract` formalizes these requirements as runnable tests.

## Contract Methods

### `test_list_mounts_returns_list`

Asserts that `list_mounts(ctx)` returns a Python `list`. This is the basic capability advertisement -- the registry calls this to discover what paths a provider serves. Returning a generator or tuple instead would break iteration logic that assumes `list` semantics.

### `test_list_entries_returns_page`

Fetches the first available mount and calls `list_entries`. Asserts that the result has an `items` attribute -- the minimal `Page` contract. If no mounts are available under the default context, the test is skipped rather than failed.

```python
page = await prov.list_entries(self.ctx(), mounts[0].path, None, 10, {})
assert hasattr(page, "items")
```

### `test_unsupported_ops_raise`

Tests that `rename`, `move`, and `delete` raise `ProviderUnsupported` for providers that do not support mutations. The test uses a `try/except ProviderUnsupported: continue` pattern, so providers that do support these ops will also pass. This makes the contract test usable for both read-only and read-write providers.

### `test_id_is_namespaced`

For every entry returned by `list_entries`, asserts that `entry.id` starts with `provider_id + ":"`. This is the global uniqueness guarantee that allows entries from multiple providers to coexist in a unified list without ID collisions.

```python
for e in page.items:
    assert e.id.startswith(prov.provider_id + ":")
```

## Usage Pattern

To add a new provider and verify compliance, a developer creates a test file with:

```python
class TestMyProvider(ProviderContract):
    def build_provider(self) -> FolderProvider:
        return MyProvider(config=...)
```

All four contract tests run automatically. This eliminates the need to manually write the same four assertions for every new provider.

## Known Gaps

The contract does not currently test `get_entry`, `open_stream`, `upload`, or `search`. These are optional or provider-specific operations and are absent from the abstract contract, meaning a provider could return malformed results from these methods without failing the compliance suite. Future iterations may add optional contract methods for providers that advertise support for these operations via their capability flags.
