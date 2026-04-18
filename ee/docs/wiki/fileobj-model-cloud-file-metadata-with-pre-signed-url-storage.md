---
{
  "title": "FileObj Model: Cloud File Metadata with Pre-Signed URL Storage",
  "summary": "Beanie document model that stores file metadata in MongoDB while actual file bytes live in S3, GCS, or local storage. Acts as the indirection layer between the application and multi-cloud object storage providers.",
  "concepts": [
    "FileObj",
    "Beanie Document",
    "S3",
    "GCS",
    "pre-signed URL",
    "MongoDB",
    "file metadata",
    "object storage",
    "multi-cloud"
  ],
  "categories": [
    "Models",
    "Cloud Storage",
    "Data Layer"
  ],
  "source_docs": [
    "7b17a00bbf26db20"
  ],
  "backlinks": null,
  "word_count": 346,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# FileObj Model: Cloud File Metadata with Pre-Signed URL Storage

## Purpose

The `FileObj` model exists to decouple file metadata from file content. Storing binary blobs directly in MongoDB would be expensive, slow, and hit the 16MB BSON document limit. Instead, actual bytes live in S3/GCS/local storage, and this document tracks where to find them.

## Design Decisions

### Multi-Provider Support
The `provider` field uses a Pydantic `Field(pattern=...)` regex constraint to restrict values to `gcs`, `s3`, or `local`. This is a validation-time guard — if the frontend or an internal caller passes an unsupported provider string, the request fails immediately rather than creating an orphaned metadata record that points nowhere.

### Indexed Owner Field
The `owner` field uses `Indexed(str)` with a `# type: ignore[valid-type]` comment. The type-ignore is necessary because Beanie's `Indexed()` wrapper confuses mypy — it returns a runtime annotation that mypy cannot resolve. The index itself ensures fast lookups when listing a user's files.

### Collection Name
The `Settings.name = "files"` maps this model to the `files` MongoDB collection explicitly, preventing Beanie from auto-generating a collection name from the class name (`FileObj` would become `file_obj`).

## Fields

| Field | Type | Purpose |
|-------|------|---------|
| `owner` | `Indexed(str)` | User ID who owns the file |
| `file_name` | `str` | Original upload filename |
| `bucket` | `str` | Storage bucket name |
| `provider` | `str` | Storage backend: `gcs`, `s3`, or `local` |
| `path_in_bucket` | `str` | Object key within the bucket |
| `mime_type` | `str` | MIME type (defaults to empty) |
| `size` | `int` | File size in bytes (defaults to 0) |
| `public` | `bool` | Whether file is publicly accessible |

## Known Gaps

- No `created_at` or `updated_at` timestamps — unlike other models that extend `TimestampedDocument`, this extends raw `Document`. File upload time is not tracked.
- No TTL or expiration field for temporary files (e.g., upload previews).
- The `public` field has no corresponding access-control logic visible in this model — enforcement must happen elsewhere.
- No file versioning support.
