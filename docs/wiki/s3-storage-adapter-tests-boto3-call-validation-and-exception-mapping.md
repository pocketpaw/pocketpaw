---
{
  "title": "S3 Storage Adapter Tests: boto3 Call Validation and Exception Mapping",
  "summary": "This module unit-tests `S3StorageAdapter` by mocking the boto3 client directly, validating that the adapter translates `put`, `open`, and `delete` operations into the correct S3 API calls and maps boto3 exceptions to PocketPaw's typed `UploadError` hierarchy. It skips automatically when boto3 is not installed.",
  "concepts": [
    "S3StorageAdapter",
    "boto3",
    "put_object",
    "get_object",
    "StorageFailure",
    "NotFound",
    "exception mapping",
    "BytesIO",
    "streaming chunks",
    "body.close()",
    "ClientError",
    "NoSuchKey",
    "importorskip"
  ],
  "categories": [
    "testing",
    "uploads",
    "S3",
    "storage",
    "boto3",
    "test"
  ],
  "source_docs": [
    "07f1695ea62dc2c4"
  ],
  "backlinks": null,
  "word_count": 417,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/uploads/test_s3_adapter.py` tests `pocketpaw.uploads.s3.S3StorageAdapter`. Rather than using moto (a full S3 mock server), it patches the boto3 client directly. This choice is deliberate: the adapter's responsibility is to translate the `StorageProtocol` methods into the correct boto3 calls and map exceptions—integration with real S3 is out of scope for unit tests.

The file begins with `pytest.importorskip("boto3")`, which skips the entire module if boto3 is not installed. This allows the core PocketPaw package to be tested without the enterprise `s3` extra.

## `_make_adapter` Helper

Constructs an `S3StorageAdapter` via `__new__` (bypassing `__init__`) and manually sets `_bucket` and `_client`. This avoids the need to provide real AWS credentials in the test environment while still exercising the adapter's methods against a real class instance.

## Test: Put Operation (`test_put_uploads_full_body_and_returns_metadata`)

Validates that `adapter.put(key, stream, mime)`:

1. Calls `client.put_object` exactly once.
2. Passes correct `Bucket`, `Key`, and `ContentType` kwargs.
3. Passes a `BytesIO` body containing all concatenated chunks (`b"hello world"`).
4. Returns a metadata object with correct `key`, `size` (11), and `mime`.

The `Body.getvalue()` assertion verifies the adapter buffers the full async stream before calling `put_object`—S3's `put_object` does not accept async iterators, so buffering is required.

## Test: Put Exception Mapping (`test_put_wraps_client_errors_as_storage_failure`)

When `client.put_object` raises any exception, the adapter must re-raise it as `StorageFailure`. This prevents boto3-specific exceptions (`botocore.exceptions.ClientError`) from leaking through the storage protocol boundary. The test asserts the `StorageFailure` message contains the original error message for debuggability.

## Test: Open/Stream (`test_open_streams_chunks`)

`adapter.open(key)` calls `client.get_object` and yields chunks from the response body:

1. `body.read` is called until it returns `b""` (EOF).
2. Chunks are concatenated correctly (`b"abcdef"`).
3. `body.close()` is called exactly once after streaming, preventing resource leaks.

The `body.close()` assertion is particularly important: an unclosed S3 response body holds an open HTTP connection, which can exhaust the boto3 connection pool under concurrent uploads.

## Test: Open Missing Key (`test_open_missing_key_raises_not_found`)

When `client.get_object` raises a `ClientError` with `Code == "NoSuchKey"`, the adapter must raise `NotFound`. This allows the API router to return 404 without catching boto3-specific exceptions. The test uses a custom `_ClientError` class to simulate the exact boto3 error structure without importing botocore.

## Known Gaps

- No test covers `delete()` if the adapter implements it.
- `put_object` with a `Body` of type `BytesIO` is tested, but multipart upload for large files (boto3's `upload_fileobj` or `TransferConfig`) is not covered—large uploads may exceed Lambda or API gateway memory limits if buffered entirely.
- The `NoSuchKey` error code is tested but `AccessDenied` and `NoSuchBucket` error codes are not, so their exception mapping is unverified.
