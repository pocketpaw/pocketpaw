---
{
  "title": "Upload Adapter Factory Tests: Environment-Driven Backend Selection",
  "summary": "This module tests `pocketpaw.uploads.factory.build_adapter`, which selects and constructs the correct storage adapter (local or S3) based on environment variables. It validates default behavior, explicit configuration, error handling for missing S3 credentials, and environment isolation to prevent test contamination from the developer's `.env` file.",
  "concepts": [
    "build_adapter",
    "upload factory",
    "LocalStorageAdapter",
    "S3StorageAdapter",
    "POCKETPAW_UPLOAD_ADAPTER",
    "load_dotenv",
    "environment isolation",
    "monkeypatch",
    "S3_PRIVATE_BUCKET",
    "boto3",
    "adapter selection"
  ],
  "categories": [
    "testing",
    "uploads",
    "storage",
    "configuration",
    "factory pattern",
    "test"
  ],
  "source_docs": [
    "879142254b5d408a"
  ],
  "backlinks": null,
  "word_count": 537,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/uploads/test_factory.py` tests `pocketpaw.uploads.factory.build_adapter`, the single entry point for constructing a storage adapter at runtime. The factory reads `POCKETPAW_UPLOAD_ADAPTER` and related credentials from environment variables, allowing operators to switch between local development storage and production S3 without code changes. The tests validate default behavior, explicit configuration, error handling for missing credentials, and—critically—environment isolation to prevent the developer's local `.env` from contaminating test results.

## Environment Isolation Fixture (`_isolate_env`)

The `autouse` fixture is the most important part of this file. It executes before every test and does three things:

**1. Patches `load_dotenv`**: The factory lazily calls `dotenv.load_dotenv` inside `build_adapter` to pick up `.env` files before reading env vars. On a developer machine with S3 credentials in `.env`, this would cause the factory to construct an `S3StorageAdapter` even when tests expect `LocalStorageAdapter`. The fixture stubs `dotenv.load_dotenv` to a no-op, preventing env file leakage. It wraps the patch in a `try/except ImportError` to handle environments where `python-dotenv` is not installed.

**2. Clears all upload-related env vars**: `monkeypatch.delenv` removes seven env vars before each test: `POCKETPAW_UPLOAD_ADAPTER`, `S3_PRIVATE_BUCKET`, `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT`, `S3_ACCESS_KEY_ID`, and `S3_SECRET_ACCESS_KEY`. Each uses `raising=False` so the call is safe even when the var is already absent. Without this step, a test that sets `POCKETPAW_UPLOAD_ADAPTER=s3` would leave that var set for the next test.

**3. Keeps the factory module in scope**: `_ = factory_module` prevents Python from garbage-collecting the import while `monkeypatch` has it patched. This is a subtle but necessary detail when patching at the module level inside a local scope.

Without this fixture, tests would produce different results on machines with versus without S3 credentials configured—a classic environment-dependent, non-deterministic test failure.

## Test: Default Adapter (`test_defaults_to_local`)

When no `POCKETPAW_UPLOAD_ADAPTER` env var is set, `build_adapter(tmp_path)` must return a `LocalStorageAdapter`. This validates the safe default for development and CI environments where no S3 configuration exists. A missing default could cause CI to error on the very first upload attempt with a cryptic `KeyError`.

## Test: Explicit Local (`test_explicit_local`)

`POCKETPAW_UPLOAD_ADAPTER=local` must also produce `LocalStorageAdapter`. This tests the explicit code path separately from the default, ensuring the factory's dispatch logic handles both `None` (not set) and `"local"` (explicitly set) identically.

## Test: S3 Missing Required Credential (`test_s3_requires_bucket`)

When `POCKETPAW_UPLOAD_ADAPTER=s3` but `S3_PRIVATE_BUCKET` is absent, `build_adapter` must raise `RuntimeError` with a message mentioning `S3_PRIVATE_BUCKET`. This fast-fail prevents the application from starting with an incomplete S3 configuration—without it, the error would surface only on the first actual upload as an opaque `NoSuchBucket` from AWS.

## Test: Full S3 Configuration (`test_s3_mode_builds_s3_adapter`)

With all required S3 env vars set (`S3_PRIVATE_BUCKET`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`), `build_adapter` must return an `S3StorageAdapter`. The test is guarded by `pytest.importorskip("boto3")`, which skips it silently on OSS installs that ship without the enterprise `s3` extra. This avoids a hard `ImportError` from appearing as a test failure.

## Known Gaps

- No test covers `S3_ENDPOINT` for custom-endpoint S3-compatible services (MinIO, Cloudflare R2). A missing endpoint would not be caught until the first real API call.
- No test validates behavior for an unknown `POCKETPAW_UPLOAD_ADAPTER` value (e.g., `"gcs"`)—whether the factory raises `ValueError` or silently falls back to local is unspecified.
- The `S3_BUCKET` env var is cleared but never explicitly tested—it may be an alias for `S3_PRIVATE_BUCKET` or a legacy name whose handling is unverified.
