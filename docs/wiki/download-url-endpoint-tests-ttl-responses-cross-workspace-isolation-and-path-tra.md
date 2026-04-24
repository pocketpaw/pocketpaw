---
{
  "title": "Download URL Endpoint Tests: TTL Responses, Cross-Workspace Isolation, and Path Traversal Defense",
  "summary": "Integration tests for the `/uploads/{file_id}/download-url` endpoint added in the EE upload router, verifying that the endpoint returns a time-bounded URL with the original filename, blocks cross-workspace access with 404, rejects path-traversal IDs, and hides soft-deleted files. Uses the same FastAPI test-client pattern as `test_router.py` with header-driven identity injection.",
  "concepts": [
    "download-url endpoint",
    "presigned URL",
    "TTL",
    "cross-workspace isolation",
    "path traversal",
    "existence-hiding",
    "soft deletion",
    "FastAPI test client",
    "EE uploads router",
    "header-driven identity"
  ],
  "categories": [
    "testing",
    "uploads",
    "security",
    "cloud API",
    "test"
  ],
  "source_docs": [
    "b444286c8777069b"
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

The `/uploads/{file_id}/download-url` alias is a dedicated endpoint that returns a pre-signed or cookie-authenticated URL suitable for a browser download (with a `Save-As` filename and an expiry timestamp). This file covers four security and behavioral contracts for that endpoint.

## Test Fixture Wiring

`ee_client` builds a minimal FastAPI app with the EE uploads router, replaces the `_SVC` module-level singleton with a test service backed by a tmpfs `LocalStorageAdapter` and a `mongomock-motor` `MongoFileStore`, and overrides the `current_user_id` / `current_workspace_id` dependencies with header-reading stubs. This wiring pattern lets each test control identity by sending `x-user` and `x-workspace` headers, with no auth middleware needed.

`require_license` is overridden to a no-op because tests should not depend on a license check against a real license server.

## TTL and Filename Contract

`test_download_url_returns_ttl_and_filename` uploads a file named `report.png` and then requests its download URL. Assertions:

1. The returned `url` either ends with the file ID path or is an absolute HTTP URL (covering both the local-redirect and presigned-URL implementations)
2. `filename == "report.png"` — the original filename must survive the round-trip so browsers can present a meaningful `Save-As` dialog
3. `expires_at` is in the future but no more than one hour out — this bounds the TTL to a short window to limit the blast radius of a leaked URL

The TTL check uses `int(time.time())` rather than a fixed value so the test is not time-zone or clock-drift sensitive.

## Cross-Workspace Isolation

`test_download_url_blocks_cross_workspace` uploads a file in workspace `w-a` and requests its download URL from workspace `w-b`. The expected result is 404, not 403. This design choice (existence-hiding) means an attacker cannot probe whether a file ID exists in another workspace — a 403 would confirm existence, a 404 reveals nothing.

## Path Traversal Defense

`test_download_url_rejects_path_traversal_ids` sends `..%2F..%2Fetc%2Fpasswd` as the file ID. Because FastAPI URL-decodes path parameters before routing, the store receives the decoded string `../../etc/passwd` as a file ID. The workspace-scoped store performs a database lookup by opaque ID, which returns `None` for any string that is not a stored UUID — so the response is 404. This is a defense-in-depth pattern: the file ID is never used as a filesystem path, so traversal payloads simply miss.

## Soft-Delete Visibility

`test_download_url_404_for_deleted` uploads a file, deletes it via `DELETE /uploads/{file_id}`, and confirms the download-url endpoint returns 404. Soft-deleted files must be invisible to all read endpoints, including this alias, to prevent stale URL reuse.

## Known Gaps

The test suite does not cover the behavior when the underlying blob is missing from storage (record in DB but no file on disk). It also does not test concurrent requests to the same download-url endpoint.
