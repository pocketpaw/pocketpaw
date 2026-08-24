# ee/pocketpaw_ee/sites/artifact_store_s3.py — SP-4, the SHARED native-artifact store.
#
# Created 2026-08-24 (feat/sites-s3-artifact-store). New module. Nothing here existed
# before; the only edit outside it is ``service._default_artifact_store``, which now
# consults ``shared_artifact_store()`` before falling back to the filesystem store.
#
# WHY THIS EXISTS. ``service._FilesystemArtifactStore`` caches a built preview's
# ``{body_html, css}`` on the CONTAINER'S OWN DISK, keyed on ``(pocket_id,
# content_hash)``. That makes the cache per-replica and per-deploy: a view routed to
# replica B misses what replica A built, and a redeploy empties it entirely. A cold miss
# is a full ``bun install`` + SvelteKit build — 1-2 minutes on the prod box — so a miss is
# not "slightly slower", it is the difference between an instant preview and one the user
# gives up on. This store puts the same two-part key in blob storage instead, so the
# artifact is shared across replicas and outlives the container.
#
# ┌───────────────────────────────────────────────────────────────────────────────────┐
# │ THE PER-SITE CAPTURE KEY MUST NEVER REACH BLOB STORAGE.                            │
# └───────────────────────────────────────────────────────────────────────────────────┘
#
# ``build_job``'s header records the decision this inherits: a svelte scaffold
# substitutes the real per-site ``captureSignedKey`` into ``src/routes/api/submit/
# +server.ts``, and the ENTIRE argument that its exposure is acceptable rests on it
# living only in a container that is then destroyed. ``daytona_runner``'s header is the
# same obligation reached from the other side — that lane never snapshots a sandbox,
# because a snapshot moves an ephemeral secret into durable storage.
#
# S3 is durable in exactly the way both of those were avoiding, and unlike a sandbox it
# is not destroyed on any schedule we control. So the write path REFUSES a payload
# carrying a capture-key-shaped value (see ``carries_capture_key``) rather than storing
# it. It is not hypothetical that one can appear: ``generator_client
# ._resolve_capture_tokens`` substitutes ``__CAPTURE_SIGNED_KEY__`` into EVERY text entry
# of a source map, and ``_rewire_legacy_submit_forms`` emits
# ``<input type="hidden" name="paw_key" value="__CAPTURE_SIGNED_KEY__">`` into any form
# still aimed at the removed ``/api/submit`` route. Both land in the rendered page body,
# which is precisely what ``_read_native_artifact`` extracts into ``body_html``.
#
# TODAY THAT VALUE IS A THROWAWAY, AND THAT IS NOT THE POINT. ``service
# ._build_native_artifact`` builds with ``capture_signed_key=f"site_key_{token_urlsafe
# (24)}"`` — freshly minted per build, never the site's real key ("cosmetic here", says
# the call site). So what the guard refuses today is a decoy. It is still the right
# refusal: the publish path threads the REAL key through the same builder, the two paths
# are one parameter apart, and a guard that only fires once the secret is already durable
# has fired too late. The gap between "the decision was made" and "the code does it" is
# one grep, and daytona_runner's header exists because nobody ran it.
#
# THE COST IS REAL AND IS ACCEPTED. A site whose rendered body carries a key-shaped
# value never caches here — every view of it rebuilds. That degradation is the store's
# own contract (a refusal reads as a MISS, and a MISS rebuilds), it is logged per pocket
# so an operator can see the cache is off and why, and the alternatives are worse:
# blanking the value would serve a form that posts an empty ``paw_key`` and silently
# drops every lead, and re-tokenising would need the caller to hand the key back on read
# — a change to the ``_store`` seam that this task deliberately does not make.
#
# WHAT THIS IS NOT. It does NOT go through ``EEUploadService``: that takes a FastAPI
# ``UploadFile``, mints a Mongo ``FileRecord``, and runs chat-membership /
# workspace-admin permission checks. An artifact store wants "put these bytes at this
# key" and none of that. It talks to the low-level ``StorageAdapter`` the upload service
# itself holds, built by ``pocketpaw.uploads.build_adapter`` — the same swap point
# ``cloud/media/storage.py`` uses.
#
# TWO ENV KNOBS, AND THEY ARE NOT THE SAME ONE:
#   * ``PAW_SITES_ARTIFACT_STORE`` (default ``filesystem``) — whether site artifacts go
#     to blob storage at all. Default keeps OSS installs and local dev byte-for-byte on
#     the prior filesystem store.
#   * ``POCKETPAW_UPLOAD_ADAPTER`` (default ``local``) — WHICH backend
#     ``build_adapter`` returns. Setting only the first one on a box whose upload
#     adapter is still ``local`` gets a local-disk adapter: no crash, no sharing, and the
#     objects land under ``~/.pocketpaw/site-artifacts/`` — the same place the filesystem
#     store writes. ``cloud/uploads/bootstrap.verify_cloud_storage_backend`` already
#     warns (or refuses to boot, under ``POCKETPAW_REQUIRE_S3_IN_CLOUD``) about exactly
#     that misconfiguration, so this module does not duplicate the guard.
#
# NO EVICTION HERE, DELIBERATELY. The filesystem store prunes to
# ``PAW_SITES_ARTIFACT_KEEP`` because a container's disk is small and shared. A bucket is
# neither, and doing it in code would cost a LIST + DELETE round-trip on every write to
# solve a problem object storage already has a native answer for. Put a lifecycle rule on
# the ``site-artifacts/`` prefix instead.
"""SP-4 — a blob-storage-backed native-artifact store for Paw Sites previews."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator, Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Key prefix inside the bucket. The two components after it are ``pocket_id`` and
#: ``content_hash`` — the SAME key the filesystem store uses, in the same order, so the
#: two stores address one artifact identically and a deployment can switch between them
#: without invalidating anything.
ARTIFACT_KEY_PREFIX = "site-artifacts"

#: Local root handed to ``build_adapter`` for the LOCAL-adapter case. Mirrors
#: ``cloud/media/storage.MEDIA_ROOT``; combined with the prefix above it resolves to
#: ``~/.pocketpaw/site-artifacts/<pocket_id>/<hash>.json`` — the filesystem store's own
#: layout, so a half-configured box degrades onto the path it was already using.
LOCAL_ADAPTER_ROOT = Path.home() / ".pocketpaw"

_MODE_FILESYSTEM = "filesystem"
_MODE_S3 = "s3"

#: A per-site capture key is ``site_key_`` + ``secrets.token_urlsafe(24)`` (~32 chars of
#: the URL-safe alphabet) — minted that way in ``service`` (five call sites) and in
#: ``cloud/models/site.Site``. The 16-char floor is borrowed from
#: ``cloud/auth/site_keys._MIN_SITE_KEY_LEN`` and for the same reason: it is comfortably
#: above short junk and below a real key, so it survives a future token-length tweak.
_CAPTURE_KEY_RE = re.compile(r"site_key_[A-Za-z0-9_-]{16,}")

#: Seconds a single blob round-trip may block the caller. The ``_store`` seam is SYNC and
#: is called from inside the request's event loop, so an unbounded wait does not slow one
#: preview down — it wedges the loop. Ten seconds is far above a healthy PUT/GET of a
#: page-sized JSON and far below "the user has already left".
_TIMEOUT_DEFAULT_SEC = 10.0


def artifact_store_mode() -> str:
    """Which native-artifact store this process should use.

    ``filesystem`` (the default, and anything unrecognised) keeps the prior on-disk
    store. ``s3`` routes artifacts through the configured ``StorageAdapter``.
    """
    return (os.environ.get("PAW_SITES_ARTIFACT_STORE") or _MODE_FILESYSTEM).strip().lower()


def _timeout_sec() -> float:
    """Per-call blob round-trip budget, overridable with
    ``PAW_SITES_ARTIFACT_S3_TIMEOUT_SEC``. A non-numeric or non-positive value falls
    back to the default rather than disabling the bound."""
    raw = os.environ.get("PAW_SITES_ARTIFACT_S3_TIMEOUT_SEC")
    try:
        value = float(raw) if raw else _TIMEOUT_DEFAULT_SEC
    except (TypeError, ValueError):
        return _TIMEOUT_DEFAULT_SEC
    return value if value > 0 else _TIMEOUT_DEFAULT_SEC


def carries_capture_key(*values: str) -> bool:
    """Whether any value carries a per-site capture-key-shaped token.

    The one gate between a rendered artifact and durable storage — see the module
    header for why the answer to "is this really a secret, or the build-time decoy?"
    is deliberately not asked. A key-shaped value is treated as a key.
    """
    return any(_CAPTURE_KEY_RE.search(value) for value in values if isinstance(value, str))


def artifact_key(pocket_id: str, content_hash: str) -> str:
    """The blob key for one artifact. Same ``(pocket_id, content_hash)`` pair the
    filesystem store puts in its path, under a fixed prefix."""
    return f"{ARTIFACT_KEY_PREFIX}/{pocket_id}/{content_hash}.json"


def _run_coro(coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
    """Drive a coroutine to completion from SYNCHRONOUS code, bounded by ``timeout``.

    Mirrors ``pocketpaw.__main__._run_async`` / ``discovery.kb_compile._run_coro``, plus
    a deadline. The ``_store`` seam is sync (``store.read(...)`` / ``store.write(...)``
    with no ``await``) while ``StorageAdapter`` is async, and both call sites sit inside
    a running loop — so ``asyncio.run`` here would raise "cannot be called from a running
    event loop". The coroutine therefore runs on a worker thread with its own loop.

    The executor is NOT used as a context manager on purpose: ``__exit__`` waits for the
    worker, which would make the timeout meaningless. On a timeout the thread is left to
    finish on its own (boto3 carries its own socket timeouts) and the caller degrades to
    a MISS.
    """
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paw-artifact-blob")
    try:
        return executor.submit(asyncio.run, coro).result(timeout=timeout)
    finally:
        executor.shutdown(wait=False)


async def _read_all(stream: AsyncIterator[bytes]) -> bytes:
    """Drain an adapter byte stream. Artifacts are page-sized, so buffering is fine."""
    chunks: list[bytes] = []
    async for chunk in stream:
        chunks.append(chunk)
    return b"".join(chunks)


async def _bytes_stream(data: bytes) -> AsyncIterator[bytes]:
    """Adapt an in-memory payload into the async byte stream ``StorageAdapter.put``
    consumes. Mirrors ``cloud/media/storage.bytes_stream``."""
    yield data


class S3ArtifactStore:
    """Read-through native-artifact store backed by a ``StorageAdapter``.

    Satisfies the same duck-typed ``_store`` seam as ``service._FilesystemArtifactStore``
    — ``read(pocket_id, content_hash)`` and ``write(pocket_id, content_hash, body_html,
    css)``, both SYNC — so ``get_native_artifact`` and the pre-warm are unchanged.

    Best-effort on BOTH sides, matching the filesystem store exactly. A miss, a corrupt
    or truncated object, a timeout, an unreachable bucket, denied credentials: every one
    of them returns ``None`` so the caller rebuilds. A failed write is swallowed and
    logged. Nothing raised in here may break a preview, so the ``except Exception``
    breadth is the contract rather than laziness — the alternative is enumerating every
    botocore error a bucket can produce and turning the one it missed into a 500.

    Tenant isolation is the filesystem store's, unchanged: the key is built from a
    ``pocket_id`` resolved by a tenant-scoped pockets read, so one tenant's prefix is not
    addressable from another tenant's request.
    """

    def __init__(self, adapter: Any, *, timeout: float | None = None) -> None:
        self._adapter = adapter
        self._timeout = timeout if timeout and timeout > 0 else _timeout_sec()

    def read(self, pocket_id: str, content_hash: str) -> tuple[str, str] | None:
        key = artifact_key(pocket_id, content_hash)
        try:
            raw = _run_coro(_read_all(self._adapter.open(key)), self._timeout)
        except Exception:
            # A miss is the common case here (``NotFound`` on a cold key), so this is
            # debug, not warning — a warning per cache miss would bury the write
            # failures that actually want an operator.
            logger.debug("sites.artifact_store_s3: read miss for %s", key, exc_info=True)
            return None
        try:
            data = json.loads(raw)
            body_html = data["body_html"]
            css = data["css"]
        except (ValueError, KeyError, TypeError):
            logger.debug("sites.artifact_store_s3: corrupt artifact at %s", key)
            return None
        if not isinstance(body_html, str) or not isinstance(css, str):
            return None
        return body_html, css

    def write(self, pocket_id: str, content_hash: str, body_html: str, css: str) -> None:
        if carries_capture_key(body_html, css):
            # Refused, not scrubbed. See the module header: the whole reason this key's
            # exposure was ever acceptable is that it does not outlive its container.
            logger.warning(
                "sites.artifact_store_s3: refusing to store an artifact for pocket %s — "
                "the rendered payload carries a per-site capture key, which must never "
                "reach durable blob storage. This pocket's preview will rebuild on every "
                "view until the key stops being substituted into its rendered body.",
                pocket_id,
            )
            return

        key = artifact_key(pocket_id, content_hash)
        payload = json.dumps(
            {
                "body_html": body_html,
                "css": css,
                "stored_at": datetime.now(UTC).isoformat(),
            }
        ).encode("utf-8")
        try:
            _run_coro(
                self._adapter.put(key, _bytes_stream(payload), "application/json"),
                self._timeout,
            )
        except Exception:
            # Best-effort cache — a write failure must not break the render path.
            logger.warning(
                "sites.artifact_store_s3: write failed for pocket %s", pocket_id, exc_info=True
            )


# --------------------------------------------------------------------------- #
# Process-wide resolution
# --------------------------------------------------------------------------- #
#
# The adapter is cached because building one constructs a boto3 client; the store
# wrapper around it is trivial and is rebuilt per call so an env change to the timeout
# takes effect without a restart. A failed build is cached as a failure too — a box with
# no S3 credentials must not attempt (and log) a fresh construction on every preview.

_adapter: Any | None = None
_adapter_failed = False


def _shared_adapter() -> Any | None:
    """The process's blob adapter, or ``None`` if one cannot be built."""
    global _adapter, _adapter_failed

    if _adapter is not None:
        return _adapter
    if _adapter_failed:
        return None
    try:
        from pocketpaw.uploads.factory import build_adapter

        _adapter = build_adapter(LOCAL_ADAPTER_ROOT)
    except Exception:
        _adapter_failed = True
        logger.warning(
            "sites.artifact_store_s3: PAW_SITES_ARTIFACT_STORE=s3 but no storage adapter "
            "could be built (check POCKETPAW_UPLOAD_ADAPTER and the S3_* settings). "
            "Falling back to the local filesystem artifact store.",
            exc_info=True,
        )
        return None
    return _adapter


def shared_artifact_store() -> S3ArtifactStore | None:
    """The shared artifact store for this process, or ``None`` to keep the filesystem one.

    ``None`` is the default and the fallback: unset / unrecognised
    ``PAW_SITES_ARTIFACT_STORE``, or ``s3`` on a box where no adapter can be built. The
    caller (``service._default_artifact_store``) treats ``None`` as "use the prior
    store", so a misconfiguration degrades to today's behaviour instead of failing a
    preview.
    """
    if artifact_store_mode() != _MODE_S3:
        return None
    adapter = _shared_adapter()
    if adapter is None:
        return None
    return S3ArtifactStore(adapter)


def reset_shared_adapter() -> None:
    """Drop the cached adapter. For tests that flip the env between cases."""
    global _adapter, _adapter_failed

    _adapter = None
    _adapter_failed = False


__all__ = [
    "ARTIFACT_KEY_PREFIX",
    "LOCAL_ADAPTER_ROOT",
    "S3ArtifactStore",
    "artifact_key",
    "artifact_store_mode",
    "carries_capture_key",
    "reset_shared_adapter",
    "shared_artifact_store",
]
