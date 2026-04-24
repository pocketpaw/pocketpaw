---
{
  "title": "Cloud Files Tree Builder Tests: Ordering, Nesting, and Resilient Provider Failures",
  "summary": "This module tests `build_tree`, which assembles a hierarchical folder tree from all registered provider mounts. Key behaviors tested include mount ordering by the `order` field, recursive path-segment nesting, and graceful warning collection when individual providers fail rather than aborting the entire tree build.",
  "concepts": [
    "build_tree",
    "FolderNode",
    "mount ordering",
    "path segment nesting",
    "provider failure resilience",
    "tree warnings",
    "FailingProvider",
    "list_mounts",
    "virtual filesystem tree",
    "collect_warnings"
  ],
  "categories": [
    "Cloud Files",
    "Testing",
    "Tree Building",
    "Resilience",
    "test"
  ],
  "source_docs": [
    "6fe72fd38cb08c65"
  ],
  "backlinks": null,
  "word_count": 530,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/cloud/files/test_tree.py` covers `build_tree` from `ee.cloud.files.tree`. This function aggregates mounts from all registered providers, sorts them, and assembles a nested `FolderNode` hierarchy that the frontend renders as a folder sidebar. The tests validate correctness under both normal operation and partial provider failure.

## What `build_tree` Does

`build_tree` fans out to every registered `FolderProvider`, calls `list_mounts` on each, collects the resulting `ResolvedMount` objects, sorts them by `order`, and constructs a tree by splitting each mount path into segments and nesting `FolderNode` objects accordingly.

The result is a virtual filesystem tree that is independent of any physical storage -- `/My Files`, `/Workspaces/ws_1/KB`, and `/Connected/Drive` can all appear as siblings or nested nodes regardless of which underlying provider serves each one.

## Test Breakdown

### `test_build_tree_merges_mounts_sorted_by_order`

Registers two providers -- `uploads` at `/My Files` (order 10) and `kb` at `/Workspaces/ws_1/KB` (order 20). Asserts that the top-level children of the resulting tree are `["My Files", "Workspaces"]`, in ascending order.

```python
assert [c.name for c in tree.children] == ["My Files", "Workspaces"]
```

The `order` field controls sidebar appearance: lower order means higher in the list. If `build_tree` sorted by insertion order or alphabetically instead, the tree would be non-deterministic across restarts and YAML editors.

### `test_build_tree_nests_segments`

A single mount at `/Workspaces/ws_1/KB` must produce a three-level tree: `Workspaces` -> `ws_1` -> `KB`. The test traverses the resulting tree structure to confirm each segment is a separate `FolderNode`.

```python
assert tree.children[0].name == "Workspaces"
assert tree.children[0].children[0].name == "ws_1"
assert tree.children[0].children[0].children[0].name == "KB"
```

This nesting behavior allows workspace-scoped mounts to appear under a logical `Workspaces/` folder without any special-casing in the provider -- the tree builder derives structure purely from path string parsing.

### `test_build_tree_returns_warnings_on_provider_failure`

This test introduces a `FailingProvider` that raises `RuntimeError("boom")` from `list_mounts`. The `uploads` provider succeeds; the `kb` provider fails. The test calls `build_tree` with `collect_warnings=True` and asserts:

1. The tree contains only `My Files` (from the successful provider).
2. The warnings list contains one entry: `{"provider_id": "kb", "code": "files.provider_error"}`.

```python
tree, warnings = await build_tree(
    ctx=ctx, registry=reg, rules=AbacRuleSet(), collect_warnings=True
)
assert [c.name for c in tree.children] == ["My Files"]
assert warnings == [{"provider_id": "kb", "code": "files.provider_error"}]
```

This is the key resilience pattern: a single provider outage must not prevent the entire tree from rendering. Users can still browse the mounts that are available, and the frontend can display a specific warning for the provider that is down (e.g., "Google Drive is temporarily unavailable").

Without this behavior, a transient network error to one provider would produce a full 500 response, leaving users unable to browse any files.

## Test Infrastructure

The `_clear_tree_cache` fixture (autouse) from the conftest ensures the in-process tree cache does not carry state between tests. Without it, a tree built by an earlier test could mask a failure in a later test that expects a fresh build.

## Known Gaps

There is no test for the case where two mounts from different providers share the same path prefix (e.g., both mounted at `/Workspaces`). The behavior -- whether the tree merges them into a single node or creates duplicates -- is not specified here. Additionally, the `collect_warnings=False` default path (where provider failures are silently suppressed rather than collected) is not tested.
