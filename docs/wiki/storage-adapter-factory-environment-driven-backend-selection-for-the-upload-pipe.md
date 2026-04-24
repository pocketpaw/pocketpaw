---
{
  "title": "Storage Adapter Factory: Environment-Driven Backend Selection for the Upload Pipeline",
  "summary": "The `factory.py` module provides a single `build_adapter()` function that reads the `POCKETPAW_UPLOAD_ADAPTER` environment variable and returns either a local disk adapter or an S3-compatible adapter, enabling backend swaps without config file changes. A defensive `load_dotenv()` call at build time ensures environment variables are visible even when the factory runs before the application lifecycle loads `.env`.",
  "concepts": [
    "build_adapter",
    "POCKETPAW_UPLOAD_ADAPTER",
    "S3StorageAdapter",
    "LocalStorageAdapter",
    "factory pattern",
    "load_dotenv",
    "environment-driven configuration",
    "S3_PRIVATE_BUCKET",
    "lazy import",
    "idempotent"
  ],
  "categories": [
    "uploads",
    "storage",
    "configuration",
    "deployment"
  ],
  "source_docs": [
    "7b4400febe59b86d"
  ],
  "backlinks": null,
  "word_count": 376,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw deployments range from single-developer laptops (local disk storage) to cloud instances (S3). The upload pipeline should not require code changes to switch between them -- only environment configuration. `factory.py` implements this using a factory function pattern: one entry point that reads the environment and returns the correct `StorageAdapter` implementation.

## The build_adapter Function

```python
def build_adapter(local_root: Path) -> StorageAdapter:
    kind = os.environ.get("POCKETPAW_UPLOAD_ADAPTER", "local").strip().lower()
    if kind == "s3":
        return _build_s3()
    return LocalStorageAdapter(root=local_root)
```

The default is `"local"`, so deployments that do not set `POCKETPAW_UPLOAD_ADAPTER` get on-disk storage. The `strip().lower()` normalization prevents failures from trailing whitespace or case differences in the environment variable value.

## Defensive dotenv Loading

The factory is called at module import time by FastAPI routers, which happens before the dashboard lifecycle's `.env` loading code runs. Without a defensive `load_dotenv()` here, `POCKETPAW_UPLOAD_ADAPTER=s3` set in `.env` would be invisible at factory instantiation time -- the factory would silently fall through to the local adapter, and S3 uploads would never work.

```python
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
```

`load_dotenv()` is documented as idempotent -- calling it multiple times is safe and does not override variables already set in the process environment.

## S3 Configuration

The `_build_s3()` function reads five environment variables to configure `S3StorageAdapter`:

- `S3_PRIVATE_BUCKET` (or `S3_BUCKET`) -- required. Raises `RuntimeError` with a clear message if missing.
- `S3_REGION` -- optional, for explicit region targeting.
- `S3_ENDPOINT` -- optional, for S3-compatible endpoints (MinIO, DigitalOcean Spaces, Cloudflare R2).
- `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY` -- optional if running on EC2 with an IAM role.

The variable names deliberately match the convention used by `interacly-backend`, enabling a single set of environment variables to configure both services pointing at the same bucket.

The `S3StorageAdapter` class is imported inside `_build_s3()` rather than at the module level. This lazy import means the S3 client library is only loaded when the S3 backend is actually selected -- avoiding an `ImportError` on systems where the S3 optional extras are not installed.

## Known Gaps

The factory only supports two adapter types (`local` and `s3`). There is no plugin mechanism for registering additional adapter types without modifying the factory directly. Additionally, `POCKETPAW_UPLOAD_ADAPTER=gcs` would silently fall through to the local adapter rather than raising an error for an unrecognized value.