---
{
  "title": "Cloud Files Browse Mount Tests: ABAC Filtering and Provider Resolution",
  "summary": "This test module validates the `browse_mount` function in PocketPaw's cloud files subsystem, covering successful entry listing, ABAC tag-based filtering, and graceful errors when a mount references an unregistered provider. Together these tests protect the file-browsing surface against misconfiguration and unauthorized data exposure.",
  "concepts": [
    "browse_mount",
    "ProviderRegistry",
    "ABAC filtering",
    "AbacRuleSet",
    "MountNotFound",
    "FakeProvider",
    "mount resolution",
    "capabilities",
    "cloud files",
    "tag-based access control"
  ],
  "categories": [
    "Cloud Files",
    "Testing",
    "Access Control",
    "Provider System",
    "test"
  ],
  "source_docs": [
    "426119ebebb29555"
  ],
  "backlinks": null,
  "word_count": 534,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/files/test_browse.py` exercises the `browse_mount` coroutine, the primary entry point for listing entries under a specific virtual mount path. It sits at the intersection of three concerns: provider registration, ABAC (Attribute-Based Access Control) enforcement, and error propagation when runtime configuration is incomplete.

## Why These Tests Exist

The cloud files layer in PocketPaw uses a virtual filesystem model. Files are not stored in a flat namespace -- they are exposed through mounts that delegate to pluggable providers (e.g., `uploads`, `drive`, `kb`). A request to browse `/My Files` must resolve the `uploads` provider, fan out to it, and then apply policy before returning results to the caller.

Without tests at this level, two broad failure classes are invisible:

1. **Silent over-sharing**: ABAC rules that should hide `confidential`-tagged entries fail quietly, leaking restricted files to low-privilege users.
2. **Unhelpful 500s**: When `mounts.yaml` declares a provider that has not been wired into the `ProviderRegistry` at runtime, the system should raise `MountNotFound` (a structured 404), not an unhandled `AttributeError`.

## Test Breakdown

### `test_browse_mount_returns_entries`

The happy path: a `ProviderRegistry` is configured with one mount template (`/My Files` -> `uploads`), a `FakeProvider` is registered with a single entry, and `browse_mount` is called with an empty `AbacRuleSet` (no restrictions). The assertion checks both that exactly one item is returned and that it carries the `read` capability, confirming that the pipeline wires mount resolution, provider fan-out, and capability annotation correctly end-to-end.

```python
page = await browse_mount(
    ctx=ctx, registry=reg, rules=AbacRuleSet(),
    mount_path="/My Files", variables={}, cursor=None, limit=50, filters={}
)
assert len(page.items) == 1
assert "read" in page.items[0].capabilities
```

### `test_browse_mount_abac_filters_tagged`

This test seeds two entries -- one clean and one tagged `confidential` -- then applies an ABAC rule requiring `role=admin` for the confidential tag. The context carries `role=member`, so the tagged entry must be suppressed. The assertion confirms only the untagged entry's id appears in the response.

The defensive value here is significant: if the ABAC filter accidentally inverts its logic, this test will catch the regression immediately. The tag-based approach is intentional -- it avoids baking policy into providers and keeps access rules composable and externally configurable.

### `test_browse_mount_unregistered_provider_raises_mount_not_found`

This test reproduces a realistic deployment gap. `mounts.yaml` declares a `drive` provider at `/Connected/Drive`, but no `FolderProvider` implementation is registered for `drive` in the `ProviderRegistry`. The test confirms that `browse_mount` raises `MountNotFound` rather than propagating a raw `KeyError` or `AttributeError`.

The comment in the source is explicit: "mounts.yaml may list providers whose implementations aren't wired yet." This is a real operational scenario during incremental rollouts -- new mount configurations are deployed before the provider code ships.

## Supporting Infrastructure

All three tests use the shared `ctx` and `make_entry` fixtures from `tests/cloud/files/conftest.py`, and rely on `FakeProvider` -- a synchronous stub that returns a predetermined list of entries without hitting any external service. This isolation makes the tests fast, deterministic, and safe to run in CI without network access.

## Known Gaps

No explicit `TODO` or `FIXME` markers appear in this file, but the test coverage has one notable gap: there are no tests for cursor-based pagination through `browse_mount`. If `FakeProvider` returns more entries than `limit`, the behavior of `next_cursor` is not exercised here. Pagination correctness is presumably covered elsewhere or is a future addition.
