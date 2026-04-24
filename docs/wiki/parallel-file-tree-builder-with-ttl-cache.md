---
{
  "title": "Parallel File Tree Builder with TTL Cache",
  "summary": "Implements the parallel fan-out logic that queries all registered providers' mount lists simultaneously, merges them into a hierarchical `FolderNode` tree, and applies ABAC mount-level filtering. `CachedTreeBuilder` wraps this with a per-user, per-workspace TTL cache to avoid hammering providers on repeated tree requests.",
  "concepts": [
    "CachedTreeBuilder",
    "build_tree",
    "asyncio.gather",
    "FolderNode",
    "_insert",
    "ABAC mount filtering",
    "TTL cache",
    "parallel fan-out",
    "injectable clock",
    "FolderProvider",
    "list_mounts",
    "path hierarchy"
  ],
  "categories": [
    "files",
    "tree",
    "caching",
    "cloud"
  ],
  "source_docs": [
    "7b89f628a0b47bec"
  ],
  "backlinks": null,
  "word_count": 483,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee.cloud.files.tree` answers the question: "what does the complete files tree look like for this user right now?" It does this by querying every registered provider in parallel, merging their mounts into a path-based hierarchy, and filtering the result through ABAC rules. The result is a `FolderNode` tree that the `GET /files/tree` endpoint returns to the frontend.

## Parallel Fan-Out

The core `build_tree` function uses `asyncio.gather` to call `list_mounts(ctx)` on every provider simultaneously. This is critical for latency: if there are four providers (uploads, KB, Drive, SharePoint) and each takes 50ms, sequential queries would take 200ms while parallel fan-out takes ~50ms.

If a single provider's `list_mounts` raises an exception, `asyncio.gather` with `return_exceptions=True` captures the exception rather than cancelling all other providers. Providers that fail return an empty mount list; the tree is built from the remaining providers. This means a broken provider degrades gracefully rather than making the entire files tree unavailable.

## ABAC Mount-Level Filtering

After collecting all mounts, the function applies the ABAC ruleset at the mount level. Today, no built-in mounts carry tags, so all mounts pass the ABAC check unconditionally. The filtering hook exists so future mounts (e.g., a "Legal Documents" mount tagged `classification:confidential`) can be hidden from non-legal users without modifying the tree builder.

## _insert: Path-Based Tree Construction

```python
def _insert(root: FolderNode, mount: ResolvedMount) -> None:
```

`_insert` splits a resolved mount path on `/` and walks (or creates) `FolderNode` children to place the mount at the correct depth. For example, a mount at `/workspaces/abc/my-files` produces a tree: root -> `workspaces` -> `abc` -> `my-files`. This means intermediate nodes (`workspaces`, `abc`) are created as structural folder nodes even if no provider directly owns them.

## CachedTreeBuilder

Building the tree requires parallel I/O across all providers. On a workspace with many active files panels, rebuilding on every request would create unnecessary load. `CachedTreeBuilder` addresses this with a per-`(user_id, workspace_id)` TTL cache:

```python
class CachedTreeBuilder:
    def __init__(self, *, registry, rules, ttl_seconds: int, clock: Callable[[], float]) -> None:
```

The `clock` parameter is injectable -- tests pass a fake clock to control TTL expiry without sleeping. The cache maps `(user_id, workspace_id)` tuples to `(tree, expiry_timestamp)` pairs. Expired entries are rebuilt on next access.

The TTL is configurable at construction time (typically 30-60 seconds). A stale tree means new mounts added by other sessions won't appear immediately, which is an acceptable trade-off for a UI feature that users typically view briefly rather than poll continuously.

## Known Gaps

- **No cache invalidation on mutation.** When a user uploads a file or a new workspace is created, the cached tree is not invalidated. Users may see stale trees until TTL expires.
- **Intermediate structural nodes have no provider.** Nodes like `workspaces` and the workspace ID segment exist only for tree structure. If the UI attempts to browse one of these structural nodes, no provider owns it and `resolve_mount` will raise `MountNotFound`.