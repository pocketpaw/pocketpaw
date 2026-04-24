---
{
  "title": "UploadsProvider Tests: Field Mapping, Mounts, RBAC, and Folder Listing",
  "summary": "This module tests the UploadsProvider, which surfaces user file uploads as a virtual file tree with folder hierarchy support. Tests cover field mapping from the raw upload store format to FileEntry, mount resolution, baseline RBAC (owner vs. non-owner vs. admin), and the combined folder+file listing behavior.",
  "concepts": [
    "UploadsProvider",
    "virtual file tree",
    "FileEntry",
    "field mapping",
    "ResolvedMount",
    "baseline RBAC",
    "owner permissions",
    "admin permissions",
    "folder hierarchy",
    "_StubFolders",
    "ProviderContract",
    "async iterator"
  ],
  "categories": [
    "testing",
    "files",
    "RBAC",
    "upload management",
    "test"
  ],
  "source_docs": [
    "tests/cloud/files/providers/test_uploads_provider.py"
  ],
  "backlinks": null,
  "word_count": 491,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_uploads_provider.py` covers `UploadsProvider`, the provider that makes user-uploaded files accessible through the unified virtual file system. It uses a `_StubFolders` in-memory folder store and the shared `ProviderContract` for protocol compliance.

## _StubFolders

```python
class _StubFolders:
    """In-memory folder store stub with the two methods the provider uses."""
    def __init__(self, folders=None):
        self._by_parent = folders or {}

    async def list_children_folders(self, workspace, parent_path):
        return self._by_parent.get(parent_path, [])

    async def get_by_id(self, workspace, folder_id):
        for kids in self._by_parent.values():
            for f in kids:
                if f.folder_id == folder_id:
                    return f
        return None
```

`_StubFolders` provides the two folder-store methods `UploadsProvider` calls. The internal representation is a dict from parent path to list of child folders, mirroring a real folder hierarchy. `get_by_id` does a linear scan across all children, which is correct for test data sizes.

## Provider Contract Compliance

`TestUploadsProviderContract` extends `ProviderContract` and wires a `MagicMock`-based upload store with async iterator support via an `_iter` coroutine:

```python
class TestUploadsProviderContract(ProviderContract):
    def build_provider(self):
        store = MagicMock()
        async def _iter(workspace_id, *, include_deleted=False, limit=500):
            ...
        store.iter_by_workspace = _iter
        return UploadsProvider(store=store, folders=_StubFolders())
```

The `_iter` async generator stub satisfies the upload store's streaming interface without requiring a real database.

## Field Mapping

`test_uploads_provider_list_entries_maps_fields` creates a raw upload record (as the store would return) and asserts that the resulting `FileEntry` has the correct field values: `id`, `title`, `mime`, `size`, `owner_id`, `created_at`, etc. This test pins the mapping transformation between the internal upload schema and the public `FileEntry` schema. If the upload store changes its field names, this test catches the regression.

## Mount Resolution

`test_uploads_provider_list_mounts_when_ctx_has_workspace` verifies that `list_mounts` returns exactly one `ResolvedMount` when the context includes a workspace ID. The mount path must include the workspace ID so that `list_entries` calls can be scoped correctly. A missing or malformed workspace ID in the mount path would break the virtual tree structure.

## Baseline RBAC

Three RBAC scenarios are covered:

### Owner Is Manage
`test_uploads_provider_baseline_rbac_owner_is_manage` asserts that the file's owner (matched by `owner_id`) receives `Permission.MANAGE`. Owners can rename, move, and delete their own files.

### Non-Owner Is Read-Only
`test_uploads_provider_baseline_rbac_non_owner_is_read_only` asserts that a different user (same workspace, no special role) receives `Permission.READ`. Uploads are personal by default; other users can view but not modify.

### Admin Manages Other Users' Files
`test_uploads_provider_admin_manage_on_other_user_folder` asserts that a context with `role="admin"` receives `Permission.MANAGE` even for files they do not own. Admins need management rights for moderation and compliance purposes.

## Folder + File Side-by-Side Listing

`test_uploads_provider_lists_folders_and_files_side_by_side` sets up a folder hierarchy alongside uploaded files and asserts that `list_entries` returns both folders and files interleaved in the correct order. This tests the provider's ability to merge two data sources (the folder store and the upload store) into a single coherent listing. Without this, the virtual file tree would show either folders or files but not both.

## Known Gaps

No TODOs or FIXMEs are present. The stub's `_iter` implementations in `TestUploadsProviderContract` vary by test case — the three overloads visible in the AST suggest different data configurations for different contract sub-tests.
