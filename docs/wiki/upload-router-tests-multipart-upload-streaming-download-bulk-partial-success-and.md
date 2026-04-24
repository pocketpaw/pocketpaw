---
{
  "title": "Upload Router Tests: Multipart Upload, Streaming Download, Bulk Partial Success, and Delete",
  "summary": "This module tests PocketPaw's `/api/v1/uploads` FastAPI router, covering single-file round-trips, bulk partial success (some files fail validation), download with correct Content-Type headers, and soft-delete followed by 404. It validates the full HTTP contract of the upload API.",
  "concepts": [
    "upload router",
    "multipart upload",
    "FastAPI",
    "TestClient",
    "UploadService",
    "partial success",
    "Content-Disposition",
    "soft delete",
    "monkeypatch",
    "bulk upload",
    "MIME validation",
    "_SVC"
  ],
  "categories": [
    "testing",
    "uploads",
    "API routes",
    "FastAPI",
    "HTTP",
    "test"
  ],
  "source_docs": [
    "2125308c1e0d4e28"
  ],
  "backlinks": null,
  "word_count": 431,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/uploads/test_router.py` tests the FastAPI router at `pocketpaw.api.v1.uploads`. The router is the HTTP surface of the upload subsystem; these tests validate the full request/response cycle including multipart parsing, header correctness, error codes in bulk responses, and resource deletion.

## Test Client Fixture

The `client` fixture is the most complex part of this file. It cannot simply import the router and wrap it in a `FastAPI` app because the router's module-level `_SVC` (upload service) is initialized at import time with default paths. Instead, the fixture:

1. Imports `pocketpaw.api.v1.uploads` as `uploads_module`.
2. Constructs a `UploadSettings`, `LocalStorageAdapter`, `JSONLFileStore`, and `UploadService` all pointing at `tmp_path / "u"`.
3. Uses `monkeypatch.setattr(uploads_module, "_SVC", test_svc)` to replace the module-level service.
4. Mounts the router into a fresh `FastAPI` app with the correct prefix.

This approach tests the real router code without any mocking of the service layer, giving high-fidelity coverage. The `monkeypatch.setattr` cleanup ensures no state leaks between tests.

## Test: Single File Round-Trip (`test_upload_single_roundtrip`)

The canonical happy path:

1. POST a PNG file via `multipart/form-data` to `/api/v1/uploads`.
2. Assert `200` and that the response contains `uploaded[0]` with correct `filename` and `mime`.
3. Extract the returned `id`.
4. GET `/api/v1/uploads/{id}`.
5. Assert `200`, correct binary content, correct `Content-Type` header, and `inline` in `Content-Disposition`.

The `Content-Disposition: inline` assertion matters for image files: browsers will render inline images rather than prompting a download, which is the correct UX for agent-generated image attachments.

## Test: Bulk Partial Success (`test_bulk_upload_partial_success`)

Two files are uploaded in a single POST: a valid PNG and an SVG (unsupported MIME). The response must be `200` with:

- `uploaded` containing one entry (the PNG).
- `failed` containing one entry with `code == "unsupported_mime"`.

This tests that the router does not short-circuit on the first failure—it processes all files and reports per-file results. The `200` status for a partial success is a deliberate API design choice: a `207 Multi-Status` would be more precise but less common in REST APIs.

## Test: Delete Then Get (`test_delete_then_get_not_found`)

1. Upload a file.
2. DELETE `/api/v1/uploads/{id}`.
3. GET the same ID → must return `404`.

This validates the soft-delete path end-to-end: the delete route calls `store.soft_delete()`, and the subsequent GET route calls `store.get()` which returns `None`, triggering the 404 response.

## Known Gaps

- No test covers uploads that exceed the maximum file size limit (`TooLarge` error path).
- No test verifies authentication/authorization—the router may require a bearer token in production that is absent here, meaning the tests do not validate the auth-gated surface.
- The `Content-Disposition` test only checks `"inline"` substring; the full header value including `filename=` is not asserted.
