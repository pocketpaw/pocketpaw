"""Pick a StorageAdapter based on environment.

Selection is env-driven rather than config-driven so deployments can swap
backends without editing the JSON config file:

- ``POCKETPAW_UPLOAD_ADAPTER=local`` (default) — on-disk storage
- ``POCKETPAW_UPLOAD_ADAPTER=s3`` — S3 or any S3-compatible endpoint

S3 mode reads the same env vars that interacly-backend uses, so a single
deployment can point both services at the same bucket:

    S3_ENDPOINT             (optional — omit for AWS public S3)
    S3_REGION
    S3_ACCESS_KEY_ID
    S3_SECRET_ACCESS_KEY
    S3_PRIVATE_BUCKET       (required in s3 mode)
"""

from __future__ import annotations

import os
from pathlib import Path

from pocketpaw.uploads.adapter import StorageAdapter
from pocketpaw.uploads.local import LocalStorageAdapter


def build_adapter(local_root: Path) -> StorageAdapter:
    """Return the configured adapter. Defaults to local-disk."""
    # Routers instantiate adapters at module import time, which happens before
    # the dashboard lifecycle loads .env. Call ``load_dotenv`` defensively so
    # S3_* / POCKETPAW_UPLOAD_ADAPTER are visible here regardless of order.
    # ``load_dotenv`` is idempotent and won't override vars already in env.
    try:  # pragma: no cover — trivial guard
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    kind = os.environ.get("POCKETPAW_UPLOAD_ADAPTER", "local").strip().lower()
    if kind == "s3":
        return _build_s3()
    return LocalStorageAdapter(root=local_root)


def _build_s3() -> StorageAdapter:
    from pocketpaw.uploads.s3 import S3StorageAdapter

    bucket = os.environ.get("S3_PRIVATE_BUCKET") or os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("POCKETPAW_UPLOAD_ADAPTER=s3 requires S3_PRIVATE_BUCKET to be set")
    return S3StorageAdapter(
        bucket=bucket,
        region=os.environ.get("S3_REGION") or None,
        endpoint_url=os.environ.get("S3_ENDPOINT") or None,
        access_key_id=os.environ.get("S3_ACCESS_KEY_ID") or None,
        secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY") or None,
    )
