---
{
  "title": "EE Upload Router Integration Tests: Roundtrip, Workspace Isolation, Owner-Only Access, and Bulk Partial Success",
  "summary": "Integration tests for the EE uploads FastAPI router covering the full upload-download-delete cycle, cross-workspace 404 enforcement, owner-only read access within a workspace, and the partial-success bulk upload response that distinguishes accepted files from rejected MIME types.",
  "concepts": [
    "EE uploads router",
    "FastAPI",
    "upload roundtrip",
    "cross-workspace isolation",
    "owner-only access",
    "soft deletion",
    "bulk upload",
    "partial success",
    "unsupported_mime",
    "content-disposition",
    "EEUploadService"
  ],
  "categories": [
    "testing",
    "uploads",
    "FastAPI router",
    "cloud API",
    "test"
  ],
  "source_docs": [
    "5567d9d4d4c4376b"
  ],
  "backlinks": null,
  "word_count": 444,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee.cloud.uploads.router` is the FastAPI router for EE-tier file uploads. It wraps `EEUploadService` and enforces workspace scoping, owner-only access (in v1), and MIME type validation. The test fixture spins up a real FastAPI app with the router mounted and replaces all external dependencies (storage, MongoDB, auth) with test doubles.

## Fixture Wiring

`ee_client` patches the `_SVC` module-level singleton in `uploads_module` with an `EEUploadService` backed by a tmpfs `LocalStorageAdapter` and a `mongomock-motor` `MongoFileStore`. The `require_license`, `current_user_id`, and `current_workspace_id` dependencies are overridden with lambdas and header-reading stubs respectively. This wiring allows tests to control identity per-request via `x-user` and `x-workspace` headers with no real auth infrastructure.

## Upload Roundtrip

`test_upload_roundtrip` posts a PNG and then GETs it back. Assertions:

1. Upload returns `200` with a list of one uploaded file
2. The file is retrievable at `/api/v1/uploads/{id}` with `200`
3. The response body matches the original bytes exactly
4. The `content-disposition` header includes `inline` — files are served inline by default, not forced to download

This confirms the core storage and retrieval pipeline works end-to-end.

## Cross-Workspace Isolation

`test_cross_workspace_get_is_404` uploads to workspace `w1` and reads from workspace `w2`. The expected response is `404`. This test does not use `403` because the endpoint implements existence-hiding: a requester from another workspace cannot even confirm the file exists.

## Owner-Only Access (v1)

`test_cross_user_same_workspace_is_404` uploads as `alice` and reads as `bob` within the same workspace. In the v1 implementation, files are owner-only — even same-workspace peers cannot read each other's files without going through the chat-member or admin path in `_assert_can_read`. The comment `# owner-only in v1` signals this will likely be relaxed in a future version when collaborator checkers are wired at the router level.

## Soft Delete Lifecycle

`test_delete_then_get_is_404` uploads, deletes with `DELETE /api/v1/uploads/{id}` (expects `204`), then GETs (expects `404`). This validates the soft-delete lifecycle through the HTTP layer: the record remains in MongoDB but is invisible to reads.

## Bulk Partial Success

`test_bulk_partial_success` uploads two files in a single request: `good.png` (valid MIME) and `bad.svg` (rejected MIME). The response is `200` with:

- `uploaded`: list with one item (the PNG)
- `failed`: list with one item containing `code: "unsupported_mime"`

This partial-success design is important: rejecting the entire multipart request when one file has a bad MIME type would punish users who upload mixed batches. The per-file status allows the client to inform the user which files were accepted and which were not.

## Known Gaps

There are no tests for upload size limits, filename sanitization at the router level, or concurrent uploads from the same user. The `inline` content-disposition test does not verify that browsers actually render the file inline (this would require a real browser).
