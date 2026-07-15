#!/usr/bin/env python
# ensure_bucket_cors.py — apply the object-storage bucket CORS policy from code.
#
# New file (2026-07-15, CORS-1). Browsers fetching presigned URLs (chat
# attachment previews, artifact byte reads) need their origin in the bucket's
# CORS allowlist, or the cross-origin response is blocked with
# "No 'Access-Control-Allow-Origin' header". This does what
# `aws s3api put-bucket-cors` does, but reuses the S3 credentials already in the
# deploy env / pocketpaw's .env (S3_ENDPOINT, S3_REGION, S3_ACCESS_KEY_ID,
# S3_SECRET_ACCESS_KEY, S3_PRIVATE_BUCKET) via the same StorageAdapter factory
# the app uses — no separate aws CLI config needed.
#
# SHARED-BUCKET SAFETY: put_bucket_cors REPLACES the whole rule set (S3 has no
# merge), and interacly-dev-private is shared with interacly-backend. By default
# this script MERGES — it reads the current rules and preserves them, adding
# only our origin rule. Pass --replace to wipe existing rules and set ours only
# (it prints a loud warning and lists what would be lost first).
#
# Usage (from the pocketpaw repo root, with .env populated):
#   uv run python scripts/ensure_bucket_cors.py https://paw.hzd.interacly.com
#   uv run python scripts/ensure_bucket_cors.py \
#       https://paw.hzd.interacly.com http://localhost:1420
#   uv run python scripts/ensure_bucket_cors.py --replace https://paw.hzd.interacly.com
#   POCKETPAW_S3_CORS_ALLOWED_ORIGINS="https://paw.hzd.interacly.com" \
#       uv run python scripts/ensure_bucket_cors.py        # origins from env
#
# Origins come from argv, else POCKETPAW_S3_CORS_ALLOWED_ORIGINS (comma or
# whitespace separated). Prints the bucket's CORS rules before and after so the
# change is visible. Requires the credentials to carry PutBucketCORS (and
# GetBucketCORS for the before/after read).

from __future__ import annotations

import asyncio
import json
import os
import sys

# The factory reads S3_* / POCKETPAW_UPLOAD_ADAPTER; it calls load_dotenv()
# defensively, so running from the repo root picks up .env automatically.
from pocketpaw.uploads.factory import _build_s3  # noqa: E402
from pocketpaw.uploads.s3 import S3StorageAdapter


def _origins_from_env() -> list[str]:
    raw = os.environ.get("POCKETPAW_S3_CORS_ALLOWED_ORIGINS", "")
    return [o for o in raw.replace(",", " ").split() if o]


async def _main(origins: list[str], *, replace: bool) -> int:
    adapter = _build_s3()
    if not isinstance(adapter, S3StorageAdapter):  # pragma: no cover - defensive
        print("error: the configured upload adapter is not S3.", file=sys.stderr)
        print("Set POCKETPAW_UPLOAD_ADAPTER=s3 and the S3_* vars.", file=sys.stderr)
        return 2

    bucket = adapter._bucket  # noqa: SLF001 - script owns the adapter it built
    print(f"bucket: {bucket}")

    before = await adapter.get_cors()
    print("CORS before:")
    print(json.dumps(before, indent=2) if before else "  (none)")

    if replace and before:
        print(
            "\n!! --replace: the existing rules above will be REPLACED with only\n"
            "!! the origins you passed. Any other service's rules are lost.",
            file=sys.stderr,
        )

    # Merge by default (preserve others' rules); --replace writes only ours.
    await adapter.ensure_cors(origins, preserve_rules=None if replace else before)

    after = await adapter.get_cors()
    print("\nCORS after:")
    print(json.dumps(after, indent=2))
    mode = "replaced with" if replace else "merged in"
    print(f"\n{mode} {len(origins)} allowed origin(s): {', '.join(origins)}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    replace = "--replace" in args
    origins = [a for a in args if not a.startswith("--")] or _origins_from_env()
    if not origins:
        print(
            "usage: ensure_bucket_cors.py [--replace] <origin> [<origin> ...]\n"
            "   or: set POCKETPAW_S3_CORS_ALLOWED_ORIGINS and run with no args.\n"
            "Merges into existing bucket CORS by default; --replace overwrites all rules.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_main(origins, replace=replace))


if __name__ == "__main__":
    raise SystemExit(main())
