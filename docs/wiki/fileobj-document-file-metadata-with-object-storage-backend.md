---
{
  "title": "FileObj Document — File Metadata with Object Storage Backend",
  "summary": "A Beanie ODM document that stores file metadata in MongoDB while keeping actual file bytes in S3, GCS, or local storage. Each record captures the owner, storage provider, bucket path, MIME type, and public accessibility flag, acting as the registry layer for a multi-provider object storage abstraction.",
  "concepts": [
    "FileObj",
    "Beanie ODM",
    "object storage",
    "S3",
    "GCS",
    "pre-signed URL",
    "file metadata",
    "provider abstraction",
    "public flag",
    "owner",
    "file registry"
  ],
  "categories": [
    "data modeling",
    "MongoDB",
    "file storage"
  ],
  "source_docs": [
    "7b17a00bbf26db20"
  ],
  "backlinks": null,
  "word_count": 451,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`file.py` defines `FileObj`, a lightweight metadata document for files stored in object storage. The core architectural decision is explicit in the docstring: "actual bytes live in S3/GCS, not MongoDB." MongoDB is used only for the registry — the location, ownership, and metadata of files. The actual content is accessed via pre-signed URLs generated from the stored bucket path.

## Schema

```python
class FileObj(Document):
    owner: Indexed(str)
    file_name: str
    bucket: str
    provider: str = Field(pattern="^(gcs|s3|local)$")
    path_in_bucket: str
    mime_type: str = ""
    size: int = 0
    public: bool = False

    class Settings:
        name = "files"
```

## Provider Abstraction

The `provider` field uses a regex pattern to constrain values to `"gcs"`, `"s3"`, or `"local"`. The three-way split allows the application to generate the correct URL scheme depending on deployment:

- `gcs` — Google Cloud Storage (signed URLs via the GCS SDK)
- `s3` — AWS S3 or any S3-compatible store (Minio, Cloudflare R2)
- `local` — local filesystem, used in development and single-machine deployments

Storing `provider` alongside `bucket` and `path_in_bucket` means the URL generation code can be stateless — given any `FileObj`, it has everything needed to produce a download link without additional configuration lookup.

## Why Not Store Files in MongoDB

MongoDB's document size limit is 16 MB per document. Even if documents were larger, storing binary content in MongoDB would:
- Bloat the working set that MongoDB keeps in RAM
- Make backups unnecessarily large
- Prevent CDN edge caching of file content
- Require routing all download traffic through the application tier

The metadata-in-Mongo / bytes-in-object-storage pattern sidesteps all of these. MongoDB handles fast metadata queries ("what files does this user own?") while the object store handles high-throughput binary transfer.

## Ownership and Access Control

`owner` is indexed and stores a user ID. The `public` flag controls whether a URL generated for this file requires authentication — public files can be served directly from the object store's CDN, private files require a time-limited pre-signed URL.

Note that `FileObj` is distinct from `FileUpload` and `FileFolder` in `ee.cloud.uploads.models`, which handle the upload workflow (chunked upload, upload completion, folder organization). `FileObj` represents a completed, addressable file.

## Known Gaps

- No workspace-level scoping — `owner` is a user ID, not a workspace ID. This means there is no built-in way to query all files belonging to a workspace without joining through the `users` collection.
- No expiry or lifecycle management fields — there is no TTL or `deleted_at` for files that should be automatically cleaned up from both MongoDB and object storage.
- `mime_type` defaults to an empty string rather than requiring a value. A file with no MIME type will cause issues for consumers that use it to set `Content-Type` headers.