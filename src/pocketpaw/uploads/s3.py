"""S3-backed StorageAdapter.

Targets any S3-compatible endpoint (AWS S3, MinIO, Cloudflare R2, Wasabi, ...).
Uses boto3 under ``asyncio.to_thread`` — it's sync-only, so we push calls off
the event loop. The throughput cost is negligible for chat-sized uploads; in
exchange we avoid pulling in aioboto3 (which still depends on boto3).

Credentials are shared with interacly-backend's file storage module — same
env var names (``S3_ENDPOINT``, ``S3_REGION``, ``S3_ACCESS_KEY_ID``,
``S3_SECRET_ACCESS_KEY``, ``S3_PRIVATE_BUCKET``) so one deployment can point
both services at the same bucket.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from pocketpaw.uploads.adapter import StorageAdapter, StorageItem, StoredObject
from pocketpaw.uploads.errors import NotFound, StorageFailure

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 64 * 1024
# Above this, in-memory buffering in put() starts eating real RAM. Chat
# attachments default to 25 MiB, so 64 MiB gives headroom without surprise
# OOM. If max_file_bytes is raised past this, switch to multipart_upload
# instead of buffering the whole body.
_MEM_BUFFER_WARN_BYTES = 64 * 1024 * 1024


class S3StorageAdapter(StorageAdapter):
    """Store blobs in an S3 bucket.

    ``endpoint_url`` defaults to AWS public S3 when unset. Pass a custom
    endpoint for MinIO / R2 / self-hosted S3.
    """

    def __init__(
        self,
        *,
        bucket: str,
        region: str | None = None,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "boto3 is required for S3 uploads — install the `enterprise` "
                "extra or add boto3 to your environment."
            ) from exc

        if not bucket:
            raise ValueError("S3StorageAdapter requires a bucket name")

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    async def put(self, key: str, stream: AsyncIterator[bytes], mime: str) -> StoredObject:
        # boto3 expects a seekable file-like object. Upload sizes are already
        # bounded by the service-layer cap (25 MiB default), so buffering in
        # memory is fine — swap to multipart_upload later if caps grow.
        buf = io.BytesIO()
        size = 0
        async for chunk in stream:
            buf.write(chunk)
            size += len(chunk)
        if size > _MEM_BUFFER_WARN_BYTES:
            logger.warning(
                "S3 put buffering %d bytes in memory for key=%s — "
                "consider multipart_upload if max_file_bytes was raised",
                size,
                key,
            )
        buf.seek(0)

        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=buf,
                ContentType=mime,
            )
        except Exception as exc:
            raise StorageFailure(str(exc)) from exc
        return StoredObject(key=key, size=size, mime=mime)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        try:
            obj = await asyncio.to_thread(self._client.get_object, Bucket=self._bucket, Key=key)
        except Exception as exc:
            if _is_missing_key(exc):
                raise NotFound(f"missing: {key}") from exc
            raise StorageFailure(str(exc)) from exc

        body = obj["Body"]
        try:
            while True:
                chunk = await asyncio.to_thread(body.read, _CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(body.close)

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)
        except Exception as exc:
            if _is_missing_key(exc):
                return
            raise StorageFailure(str(exc)) from exc

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    def local_path(self, key: str) -> Path | None:
        return None

    async def presigned_get(self, key: str, ttl_seconds: int) -> str | None:
        try:
            return await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=int(ttl_seconds),
            )
        except Exception:
            return None

    async def list_prefix(self, prefix: str) -> list[str]:
        """Return the unique "sub-folder" names under ``prefix``.

        Uses ``list_objects_v2`` with a ``/`` delimiter so that nested
        keys are collapsed into CommonPrefixes — the same way S3 console
        shows folders. Only returns the next path segment after ``prefix``.

        Example::

            prefix = "projects/ws1/user1/"
            keys:   "projects/ws1/user1/foo/file.py"
                    "projects/ws1/user1/bar/main.go"
            result: ["foo", "bar"]
        """
        if not prefix.endswith("/"):
            prefix += "/"
        try:
            resp = await asyncio.to_thread(
                self._client.list_objects_v2,
                Bucket=self._bucket,
                Prefix=prefix,
                Delimiter="/",
                MaxKeys=1000,
            )
        except Exception:
            return []
        common = resp.get("CommonPrefixes", [])
        names: list[str] = []
        for cp in common:
            cp_prefix = cp.get("Prefix", "")
            # Strip the prefix to get just the immediate child name
            relative = cp_prefix[len(prefix) :].rstrip("/")
            if relative:
                names.append(relative)
        return names

    async def browse(self, prefix: str) -> list[StorageItem]:
        """List one directory level, returning both files and sub-folders.

        Uses ``list_objects_v2`` with a ``/`` delimiter. Sub-directories
        come from ``CommonPrefixes``, files from ``Contents`` (skipping
        the directory marker object at ``prefix`` itself).
        """
        if not prefix.endswith("/"):
            prefix += "/"
        try:
            resp = await asyncio.to_thread(
                self._client.list_objects_v2,
                Bucket=self._bucket,
                Prefix=prefix,
                Delimiter="/",
                MaxKeys=1000,
            )
        except Exception:
            return []

        seen: set[str] = set()
        items: list[StorageItem] = []

        # Sub-directories (CommonPrefixes)
        for cp in resp.get("CommonPrefixes", []):
            cp_prefix = cp.get("Prefix", "")
            name = cp_prefix[len(prefix) :].rstrip("/")
            if name and name not in seen:
                seen.add(name)
                items.append(StorageItem(name=name, is_dir=True))

        # Files (Contents) — skip the directory marker (exact match of prefix)
        for obj in resp.get("Contents", []):
            key = obj.get("Key", "")
            if key == prefix:
                continue  # skip directory marker
            name = key[len(prefix) :]
            if "/" in name or name in seen:
                continue  # nested inside a sub-folder or already listed
            seen.add(name)
            items.append(
                StorageItem(
                    name=name,
                    is_dir=False,
                    size=obj.get("Size", 0),
                )
            )

        items.sort(key=lambda x: (not x.is_dir, x.name.lower()))
        return items

    async def rename_key(self, old_key: str, new_key: str) -> None:
        """Rename by copy + delete."""
        try:
            copy_source = {"Bucket": self._bucket, "Key": old_key}
            await asyncio.to_thread(
                self._client.copy_object,
                Bucket=self._bucket,
                CopySource=copy_source,
                Key=new_key,
            )
            await self.delete(old_key)
        except Exception as exc:
            raise StorageFailure(f"rename_key failed: {exc}") from exc


def _is_missing_key(exc: Exception) -> bool:
    """True if ``exc`` looks like an S3 404. Keeps the module importable even
    when botocore isn't installed (the protocol check is duck-typed)."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = response.get("Error", {}).get("Code", "")
    return code in ("NoSuchKey", "404", "NotFound")
