---
{
  "title": "Folder CRUD Integration Tests: Auto-Chain Creation, Rename Propagation, and RBAC Enforcement",
  "summary": "Integration tests for the EE uploads folder system covering folder creation with parent requirement, duplicate detection, automatic folder chain creation on upload, cascade rename of descendant paths, safe and cascading deletion, and permission enforcement distinguishing non-admin strangers from workspace admins.",
  "concepts": [
    "FolderStore",
    "folder CRUD",
    "auto-chain creation",
    "rename propagation",
    "cascade delete",
    "soft deletion",
    "workspace admin",
    "RBAC",
    "path integrity",
    "EE uploads router"
  ],
  "categories": [
    "testing",
    "uploads",
    "folder management",
    "access control",
    "test"
  ],
  "source_docs": [
    "ad308df12319c10c"
  ],
  "backlinks": null,
  "word_count": 449,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's EE upload system supports a virtual folder hierarchy for organizing files. Folders are stored in `FolderStore` (backed by Beanie/MongoDB) and linked to uploads via a `folder_path` field. This test file validates the complete CRUD lifecycle, path propagation semantics, and access control.

## Fixture Wiring

`folder_client` extends the base `ee_client` pattern by also patching `_META`, `_FOLDERS`, and the `_is_workspace_admin` module-level function. The `admins` set is exposed on the `TestClient` instance (`client.admins`) so individual tests can grant admin rights without touching global state.

## Folder Creation Rules

`test_create_folder_root_child` verifies that `/reports` can be created directly under root and that the response includes both the full `path` and the derived `name` (`reports`).

`test_create_folder_requires_parent` attempts to create `/reports/2026` when `/reports` does not exist yet and expects a `400`. This enforces tree integrity — you cannot create a node without its parent, preventing orphaned subtrees that would be unreachable by any traversal logic.

`test_create_folder_duplicate_409` confirms that creating the same path twice returns `409 Conflict`, making the creation endpoint idempotent-safe for clients that retry.

## Auto-Chain Creation on Upload

`test_upload_auto_creates_folder_chain` uploads a file with `path=/reports/2026/q2` when none of the three folder levels exist. After the upload, attempting to create `/reports`, `/reports/2026`, or `/reports/2026/q2` returns `409` — proving the upload handler auto-created the entire chain. Without this, users would need to manually create every intermediate folder before placing a file, a poor UX.

## Rename Propagation

`test_rename_folder_rewrites_descendants` renames `/reports` to `/archive` and then checks that the previously auto-created `/reports/2026` subfolder is now at `/archive/2026`. This is the most complex invariant: renaming a folder must atomically rewrite all descendant paths to maintain the tree's consistency. Without propagation, `/archive` would exist while all its children still referenced the old `/reports/...` prefix, breaking navigation and file resolution.

## Deletion Modes

`test_delete_folder_not_empty_409` tries to delete `/a` (which has a file in it) with `cascade=false` and expects `409 Conflict` with detail `folder.not_empty`. This protects against accidental data loss when a user deletes a folder not realizing it contains files.

`test_delete_folder_cascade_softdeletes_files` uses `cascade=true` and confirms the folder is removed and the file inside is soft-deleted (subsequent `GET` returns `404`). Soft deletion is chosen over hard deletion to allow potential recovery workflows.

## RBAC on Folder Operations

`test_folder_rename_stranger_forbidden` — a user who did not create the folder cannot rename it; expects `403`.

`test_folder_rename_admin_allowed` — a workspace admin (`client.admins.add(("admin", "w1"))`) can rename any folder regardless of ownership. This is the correct administrative override pattern.

`test_patch_file_stranger_forbidden` — similarly, a non-owner cannot patch file metadata (`filename`, `folder_path`).

## Known Gaps

There is no test for moving a file to a non-existent folder path (the expected behavior — 400 or auto-create — is unspecified). There are no tests for folder listing endpoints or pagination.
