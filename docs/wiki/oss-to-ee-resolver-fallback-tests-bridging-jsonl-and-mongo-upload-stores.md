---
{
  "title": "OSS-to-EE Resolver Fallback Tests: Bridging JSONL and Mongo Upload Stores",
  "summary": "Tests for the `resolve_media_paths_any` OSS→EE fallback, which handles the hybrid scenario where uploads are stored in the EE Mongo store but chat rendering goes through the OSS JSONL lookup path. Without the fallback, EE-uploaded files would appear as broken links in OSS-rendered chat messages.",
  "concepts": [
    "resolve_media_paths_any",
    "OSS fallback",
    "JSONLFileStore",
    "MongoFileStore",
    "get_unscoped",
    "hybrid deployment",
    "media resolution",
    "soft deletion",
    "module-level singleton patching",
    "workspace isolation"
  ],
  "categories": [
    "testing",
    "uploads",
    "media resolution",
    "OSS-EE interop",
    "test"
  ],
  "source_docs": [
    "b2b78a8da03ac9bc"
  ],
  "backlinks": null,
  "word_count": 433,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw has two upload metadata stores: the OSS `JSONLFileStore` (a flat JSON-lines index file) and the EE `MongoFileStore` (workspace-scoped MongoDB). During a migration period or in hybrid deployments, a file uploaded via the EE router is only in Mongo, but chat rendering may still call the OSS resolver (`resolve_media_paths_any`). Without a fallback, those files appear as broken links.

`resolve_media_paths_any` solves this by first trying the OSS JSONL lookup, then falling back to the EE Mongo store if the OSS lookup misses.

## Why This Fallback Exists

The scenario: a user uploads a file through the EE router (stored in Mongo with a workspace scope), then sends a message with the file URL. The OSS chat stream endpoint renders that message and calls `resolve_media_paths_any` to translate the URL to a disk path. The OSS `JSONLFileStore` has no entry for this file because it was not uploaded through the OSS path. Without fallback, the URL stays unresolved and the file appears broken.

## Happy-Path Fallback

`test_fallback_resolves_from_ee_mongo_when_oss_misses` stashes a file only in Mongo (simulating an EE upload), then calls `resolve_media_paths_any` with both the OSS and EE singletons patched to point at the test instances. The result is the disk path — the fallback resolved it correctly from Mongo even though JSONL had no entry.

## Ghost ID Returns Empty

`test_fallback_returns_none_when_neither_store_has_id` uses an ID that exists in neither store. The result is an empty list — the resolver drops unresolvable URLs rather than returning them.

## Soft-Delete Respected in Fallback

`test_fallback_ignores_soft_deleted_ee_record` soft-deletes the Mongo record before resolution. The result is empty — the fallback correctly ignores soft-deleted files, consistent with the invariant enforced throughout the read layer.

## get_unscoped for Cross-Workspace Lookup

`test_get_unscoped_returns_record_across_workspaces` tests a separate API on `MongoFileStore`: `get_unscoped(file_id)` retrieves a record regardless of workspace. This method powers the fallback resolver when the workspace is not known (e.g., in the OSS chat stream context). The test confirms a file stored in `ws-private` can be found by its ID without specifying the workspace.

## Patching Strategy

All tests use `unittest.mock.patch` to replace module-level singletons (`_ADAPTER`, `_META`) in both the OSS and EE router modules. This avoids modifying global state permanently and ensures each test sees a clean, controlled configuration.

## Known Gaps

The fallback does not enforce the workspace scope when resolving from Mongo in the OSS context — `get_unscoped` returns records from any workspace. This is intentional (the OSS chat stream does not have workspace context) but means a cross-workspace leak is theoretically possible if file IDs are guessable. There are no tests for the performance impact of the double-lookup on every media path.
