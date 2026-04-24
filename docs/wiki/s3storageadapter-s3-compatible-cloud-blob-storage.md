---
{
  "title": "S3StorageAdapter: S3-Compatible Cloud Blob Storage",
  "summary": "S3StorageAdapter implements the StorageAdapter interface against any S3-compatible object store—AWS S3, MinIO, Cloudflare R2, or Wasabi—using the synchronous boto3 library pushed off the event loop via `asyncio.to_thread`. Credentials use the same environment variable names as the companion interacly-backend service, enabling shared-bucket deployments.",
  "concepts": [
    "S3StorageAdapter",
    "boto3",
    "asyncio.to_thread",
    "S3-compatible",
    "MinIO",
    "Cloudflare R2",
    "ClientError",
    "_is_missing_key",
    "StorageAdapter",
    "credentials"
  ],
  "categories": [
    "uploads",
    "storage",
    "cloud"
  ],
  "source_docs": [
    "1cab46bcf9f5c194"
  ],
  "backlinks": null,
  "word_count": 517,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`S3StorageAdapter` is the cloud-native storage backend for PocketPaw uploads. It targets the same `StorageAdapter` protocol as `LocalStorageAdapter`, so callers swap backends through configuration alone—no code changes required. The adapter is intentionally designed for chat-scale workloads: individual uploads rarely exceed a few megabytes, so throughput is not the bottleneck, and the simplicity of boto3 is worth more than the added complexity of an async S3 library.

## Why boto3 Under `asyncio.to_thread`?

The alternatives—`aioboto3` and `aiobotocore`—are wrappers around boto3 that still depend on it at runtime. They add a layer of async scaffolding without eliminating the boto3 dependency, which means the dependency footprint is the same or larger. By calling `asyncio.to_thread` around each boto3 operation, the adapter pushes the blocking I/O onto a thread-pool worker, freeing the asyncio event loop. For chat-sized files this is negligible overhead, and it avoids the maintenance burden of tracking aioboto3 compatibility with boto3 API changes.

## S3-Compatible Endpoints

The `endpoint_url` parameter defaults to `None`, which boto3 interprets as AWS's public S3 endpoint. Passing a custom URL routes calls to any S3-compatible service: MinIO for on-premise deployments, Cloudflare R2 for egress-free storage, or Wasabi for lower-cost archival. This single flag makes the adapter portable without forking.

## Credential Alignment with interacly-backend

The environment variables (`S3_ENDPOINT`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_PRIVATE_BUCKET`) are deliberately named to match the interacly-backend file storage module. A single deployment can point both services at the same bucket without duplicating or aliasing environment configuration—reducing operational surface area.

## `_is_missing_key` Helper

The private `_is_missing_key` function inspects a boto3 `ClientError` to determine whether it represents an S3 404 (key not found), rather than an auth failure, a network error, or an S3 permission issue. Boto3 surfaces all of these as `ClientError` with different `Error.Code` fields, so a simple `try/except ClientError` would swallow genuine failures. The helper keeps import-time side effects minimal: if boto3 is not installed, the module is still importable because `_is_missing_key` only imports boto3 inside the function body.

## Streaming Reads

`open` uses `get_object` and iterates the response body in chunks, yielding `bytes` to the caller. The iterator runs inside `asyncio.to_thread` to avoid blocking the event loop during large reads. The chunk size mirrors the local adapter's 64 KB constant, keeping memory usage predictable.

## Error Translation

All adapter methods translate boto3 errors into PocketPaw's `NotFound`, `AccessDenied`, or `StorageFailure` exceptions:

```python
try:
    await asyncio.to_thread(self._s3.delete_object, Bucket=self._bucket, Key=key)
except ClientError as exc:
    if _is_missing_key(exc):
        raise NotFound(key) from exc
    raise StorageFailure(str(exc)) from exc
```

This keeps the error hierarchy consistent across storage backends regardless of which one is active at runtime.

## Known Gaps

- No multipart upload support: blobs exceeding boto3's single-put limit (~5 GB) will fail. In practice, chat uploads are capped well below this threshold by the `UploadService` layer, but very large file support would require a multipart code path.
- Presigned URL generation is declared on the `StorageAdapter` base as an optional override but is not implemented in this adapter—callers must use the signing module (`uploads/signing.py`) for grant tokens instead.
- Retry logic relies entirely on boto3's built-in retry configuration; custom backoff for transient S3 throttling is not wired.
