---
{
  "title": "UploadsProvider: Personal Upload File Provider with Folder Support",
  "summary": "Wraps `MongoFileStore` to expose user-uploaded files and folders as a personal 'My Files' mount in the unified files tree. As the only provider with folder support, it uses a dedicated `folder_store` and renders directory entries with `mime = application/x-directory`.",
  "concepts": [
    "UploadsProvider",
    "MongoFileStore",
    "folder_store",
    "application/x-directory",
    "baseline_rbac",
    "ownership RBAC",
    "personal scope",
    "_mount_suffix",
    "_to_entry",
    "_folder_to_entry",
    "My Files mount",
    "folder support"
  ],
  "categories": [
    "files",
    "providers",
    "uploads",
    "cloud"
  ],
  "source_docs": [
    "3a2a64d49430657f"
  ],
  "backlinks": null,
  "word_count": 447,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`UploadsProvider` is the most feature-complete of PocketPaw's built-in providers. It wraps `ee.cloud.uploads.MongoFileStore` for file records and a separate `folder_store` for folder metadata, surfacing both as a unified personal "My Files" mount. It is the only built-in provider that supports folders -- other providers (`KbProvider`) stay flat.

## Personal Scope and Ownership-Driven RBAC

The mount is scoped to the current user within the current workspace: a user's uploads in workspace A are not visible when browsing workspace B. This scope is enforced at the `list_mounts` level -- the returned `ResolvedMount` encodes the user ID so the path prefix already isolates the user's files.

`baseline_rbac` implements a three-tier ownership model:
- **Owner** (upload's `user_id` matches `ctx.user_id`) -> full `read + write + manage`
- **Workspace admin/owner** -> `read + write + manage` (for moderation)
- **All other workspace members** -> `read` only

This means the UI shows download and preview capabilities to all workspace members, but rename, delete, and replace are available only to the file owner and admins.

## Folder Support Architecture

Folders are stored separately in a `folder_store` (a MongoDB collection distinct from file records). `list_entries` fetches both files and folders for a given path, merges them, and returns a unified `Page[FileEntry]`. Folder entries use `mime = "application/x-directory"` -- a convention borrowed from Linux MIME practice -- so the frontend can render them with folder icons and handle click-through navigation.

Folder entries have no `download` capability in their `FileEntry.capabilities` list, which prevents the download button from appearing for directories in the UI.

## _mount_suffix Helper

```python
def _mount_suffix(mount_path: str) -> str:
    # Strip the '/My Files' prefix. Returns absolute normalized path.
```

Mount paths in the files tree include the mount's root prefix (e.g., `/workspaces/abc/my-files/docs/report.pdf`). When querying `MongoFileStore`, the provider needs the path relative to the mount root (`/docs/report.pdf`). `_mount_suffix` strips the prefix and normalises the result. Without this, queries would pass full tree paths to the store, which would never match.

## _to_entry and _folder_to_entry

Two separate mapping methods handle the different shapes of file and folder documents from MongoDB. `_to_entry` maps file documents (with `size`, `mime`, `s3_key` etc.) and `_folder_to_entry` maps folder documents (with only `name` and `path`). The `application/x-directory` mime assignment lives in `_folder_to_entry`.

## Known Gaps

- **No recursive folder delete.** `delete` on a folder entry does not cascade to child files and folders. Callers must delete children first, or the folder becomes unreachable but its children remain orphaned.
- **No move between personal mounts.** `move` raises `ProviderUnsupported`. Moving files between folders within My Files is not yet implemented.
- **Folder pagination is unimplemented.** `list_entries` returns all folders at a path in one query; large directories could produce unbounded result sets.