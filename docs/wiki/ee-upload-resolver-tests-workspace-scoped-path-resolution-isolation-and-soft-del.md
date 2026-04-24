---
{
  "title": "EE Upload Resolver Tests: Workspace-Scoped Path Resolution, Isolation, and Soft-Delete Visibility",
  "summary": "Tests for `EEUploadResolver` and `resolve_media_paths_scoped`, which translate `/api/v1/uploads/{id}` URLs in message content into local filesystem paths for downstream processing. Key invariants: workspace isolation (cross-workspace lookup returns `None`), non-upload URL pass-through, soft-delete invisibility, and missing-blob safety.",
  "concepts": [
    "EEUploadResolver",
    "resolve_media_paths_scoped",
    "URL-to-path resolution",
    "workspace isolation",
    "soft deletion",
    "missing blob",
    "LocalStorageAdapter",
    "MongoFileStore",
    "FileRecord",
    "media path resolution"
  ],
  "categories": [
    "testing",
    "uploads",
    "media resolution",
    "workspace isolation",
    "test"
  ],
  "source_docs": [
    "327448d668cc3979"
  ],
  "backlinks": null,
  "word_count": 438,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

When PocketPaw's chat system processes message content (e.g., rendering a JSONL stream for a client), it needs to translate upload URLs embedded in messages into actual filesystem paths so the content can be served or processed. `EEUploadResolver` handles this for workspace-scoped EE uploads, while `resolve_media_paths_scoped` is a batch helper that applies the resolver to a list of URLs.

## _stash Helper

`_stash` is a local test helper that writes a file to the temp filesystem, saves the corresponding `FileRecord` to the `MongoFileStore`, and returns both the record and the disk path. This setup reflects the two-layer reality: metadata lives in MongoDB, bytes live on disk. Both must exist for resolution to succeed.

## Happy Path: URL to Disk Path

`test_resolve_returns_disk_path_for_scoped_upload` stashes a file in `workspace="ws-1"` and resolves its URL `/api/v1/uploads/{id}` within the same workspace. The result is the `Path` to the file on disk. This is the baseline the entire feature depends on.

## Workspace Isolation

`test_resolve_enforces_workspace_isolation` stashes in `ws-1` then resolves in `ws-other`. The result is `None` — not an error, not a disk path. The resolver returns `None` to signal "not found in this workspace" so callers can skip the URL or substitute a placeholder, rather than crashing on a missing file. The design avoids leaking that the file exists in another workspace.

## Non-Upload URL Pass-Through

`test_resolve_returns_none_for_non_upload_url` passes URLs that do not match the `/api/v1/uploads/` prefix (a local path `/already/local.pdf` and a different API path `/api/v1/files/abc`). Both return `None`, signaling the resolver does not handle them. The caller (e.g., `resolve_media_paths_scoped`) will pass these through unchanged.

## Soft-Delete Invisibility

`test_resolve_returns_none_for_soft_deleted_upload` soft-deletes a file and confirms the resolver returns `None`. This is consistent with the read-gate behavior: soft-deleted files must not appear in any read path, including URL resolution.

## Missing Blob Safety

`test_resolve_returns_none_when_blob_missing_on_disk` saves the metadata record but deletes the file from disk before resolving. The resolver returns `None` rather than raising. This defends against a data inconsistency (e.g., if the storage adapter's disk was partially cleared) — the caller can skip the broken reference rather than crashing.

## Batch Resolution

`test_resolve_media_paths_scoped_mixes_pass_drop_resolve` passes three URLs: a valid upload, a local path, and a ghost (non-existent) upload ID. The result contains only the disk path and the local path — the ghost is silently dropped. This "pass known, drop unknown" behavior keeps message rendering robust when some embedded files have been deleted or are corrupted.

## Known Gaps

There are no tests for resolving URLs that reference files from a different but accessible workspace (e.g., shared resources). The batch function drops unresolvable URLs rather than returning them as errors, which could silently hide broken references.
