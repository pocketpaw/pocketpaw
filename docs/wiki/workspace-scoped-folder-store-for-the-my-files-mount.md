---
{
  "title": "Workspace-Scoped Folder Store for the My Files Mount",
  "summary": "FolderStore is a MongoDB-backed CRUD layer for the virtual folder tree in the uploads provider's My Files feature. It handles path normalization, recursive path creation, bulk path rewrites for renames, and soft delete — all constrained to a single workspace.",
  "concepts": [
    "FolderStore",
    "FileFolder",
    "folder tree",
    "virtual filesystem",
    "soft delete",
    "path normalization",
    "ensure_chain",
    "rewrite_path_prefix",
    "workspace scoping",
    "My Files"
  ],
  "categories": [
    "uploads",
    "file management",
    "cloud EE",
    "MongoDB"
  ],
  "source_docs": [
    "03a5ed49d6b99956"
  ],
  "backlinks": null,
  "word_count": 441,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/uploads/folder_store.py` provides the `FolderStore` class, a thin Beanie wrapper around the `FileFolder` document. Folders exist only on the uploads provider — other providers (knowledge base, chat history, local filesystem) remain flat. Every method takes a `workspace` argument first, and all queries include a workspace filter to prevent cross-tenant data leaks.

## Root Folder Convention

The virtual root `/` has no corresponding database row. `get_by_path` returns `None` for `"/"` and `path_exists` returns `True` for it unconditionally. This avoids a bootstrapping problem: every workspace would need a root folder row created at workspace-creation time, and any missed creation would break the entire folder tree. The convention is enforced by the `normalize_path` utility from `paths.py`.

## Recursive Path Creation

`ensure_chain` creates every missing folder along a path in order, from the shallowest to the deepest:

```python
async def ensure_chain(self, workspace: str, owner: str, path: str) -> list[FileFolder]:
    ...
```

This is used when a user uploads a file with a new path — the router calls `ensure_chain` to materialize any missing intermediate directories before inserting the file record. Without it, a file at `/reports/2026/q1/results.csv` would fail if `/reports/2026` did not exist yet.

## Bulk Path Rewrite on Rename

`rewrite_path_prefix` updates the `path` field on every folder whose path starts with `old_prefix`, handling both the exact match and strict descendants:

```python
async def rewrite_path_prefix(self, workspace: str, old_prefix: str, new_prefix: str) -> int:
    ...
```

This enables folder renames to cascade consistently. Without this, renaming `/reports` would leave `/reports/2026` with a broken path referencing the old parent name.

## Soft Delete

`soft_delete_under_prefix` sets `deleted_at` to the current UTC timestamp on all folders under a prefix. It does not physically remove rows. This preserves the ability to audit what existed, recover from accidental deletes, and correctly handle in-flight requests that may have already resolved paths. All reads filter `deleted_at == None` to treat soft-deleted folders as invisible.

## Child Listing and Counts

`list_children_folders` returns immediate children of a given parent path (one level deep). `count_subfolders` provides a lightweight count without fetching the full documents — used to check whether a folder is empty before deletion.

## Known Gaps

- `rewrite_path_prefix` and `soft_delete_under_prefix` operate on individual documents in a loop rather than using a bulk update operation. For deeply nested trees with many folders, this could be slow and non-atomic at the multi-document level — MongoDB does not guarantee a transaction across many individual saves without explicit session and transaction usage.
- There is no `move` operation that atomically updates both the folder's own path and all its children. A rename that partially succeeds before a process crash would leave the tree in an inconsistent state.