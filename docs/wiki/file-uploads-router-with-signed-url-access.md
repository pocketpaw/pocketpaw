---
{
  "title": "File Uploads Router with Signed URL Access",
  "summary": "Implements the OSS single-user file upload system with POST (single and bulk), streaming GET with signed URL authorization, and DELETE endpoints. Uploaded files are stored under `~/.pocketpaw/uploads/` with a JSONL metadata index, and downloads are gated by short-lived HMAC-signed tokens to prevent unauthorized access.",
  "concepts": [
    "file uploads",
    "signed URL",
    "HMAC token",
    "streaming response",
    "JSONL metadata index",
    "bulk upload",
    "Content-Disposition",
    "UploadService",
    "file adapter",
    "single-user storage"
  ],
  "categories": [
    "api",
    "uploads",
    "storage",
    "security"
  ],
  "source_docs": [
    "fc3cb4e8d8198f75"
  ],
  "backlinks": null,
  "word_count": 472,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## File Uploads Router with Signed URL Access

The uploads router handles the complete lifecycle of user-attached files in PocketPaw: receiving uploads, indexing metadata, serving files for download, and cleaning up on deletion. It is designed as an OSS single-user implementation — a placeholder for a multi-tenant cloud storage backend in enterprise deployments.

### Module-Level Initialization

Unlike most routers, the uploads module initializes its service stack at module load time via module-level constants:

```python
_ROOT = Path.home() / ".pocketpaw" / "uploads"
_INDEX = _ROOT / "_idx.jsonl"
_CFG = UploadSettings(local_root=_ROOT)
_ADAPTER = build_adapter(_ROOT)
_META = JSONLFileStore(path=_INDEX)
_SVC = UploadService(adapter=_ADAPTER, meta=_META, cfg=_CFG)
```

This is intentional for an OSS single-user deployment: the paths are fixed, the adapter is always the local filesystem adapter, and there is no per-request configuration. Initializing once avoids the overhead of constructing the service chain on every request. The `_OWNER = "local"` constant makes the single-user assumption explicit — all uploads belong to the same logical owner.

### Upload Endpoint

`POST /uploads` accepts one or more files in a multipart form. The `upload_many` method handles bulk uploads atomically — if any file fails validation (size limit, MIME type), the response includes a `failed` list with error details while successfully uploaded files still appear in `uploaded`. This partial-success pattern prevents a single oversized attachment from blocking all other files in a batch.

The optional `chat_id` form field associates the upload with a conversation session, enabling the memory system to surface relevant attachments during recall.

### Signed URL Download

Direct file access requires a short-lived signed token. The flow has two steps: `GET /uploads/{file_id}/grant` mints a token using `sign_grant(file_id, ttl=DEFAULT_TTL_SECONDS)`, and `GET /uploads/{file_id}` requires that token as a `?t=` query parameter.

This design prevents unauthorized enumeration. Without the signed URL pattern, any party who guesses or observes a `file_id` could download the file by hitting the endpoint directly. The signed token binds the download to a specific file ID and expires after `DEFAULT_TTL_SECONDS`, limiting the window of opportunity if a link is leaked.

The download handler inspects the file's MIME type against `INLINE_MIMES` (a set of browser-safe types like `image/*` and `text/*`) to set `Content-Disposition: inline` vs. `attachment`. This prevents the browser from executing uploaded HTML or SVG files as live pages.

### Streaming Response

The download endpoint returns a `StreamingResponse` rather than loading the entire file into memory. For large file attachments, this prevents memory exhaustion in the server process and allows the response to begin flowing to the client before the full read completes.

### Known Gaps

The `_ADAPTER` is always the local filesystem adapter regardless of any cloud storage configuration. Swapping to an S3 or GCS adapter requires code changes rather than configuration changes. The `_idx.jsonl` index has no size cap — a deployment with many thousands of uploads would accumulate an ever-growing index file with no pruning strategy.