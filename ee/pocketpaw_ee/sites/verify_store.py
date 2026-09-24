# ee/pocketpaw_ee/sites/verify_store.py — where verification records live between runs.
#
# Created 2026-09-24 (PP-2, feat/sites-verify-pipeline). Three kinds of record, all keyed
# by ``(pocket_id, content_hash)`` — the same pair the native-artifact store uses, so a
# record is exactly as fresh as the render it describes and an edit that changes the
# source simply stops finding it:
#
#   * ``sandbox-<hash>`` — written by the WORKER (``build_job``'s preview and html-verify
#     jobs): the build and browser layers plus their scrubbed diagnostics. A UI pre-warm
#     writes one too, which is the point: the build the editor paid for also answers the
#     agent's next ``verify_site`` without a second sandbox.
#   * ``verdict-<hash>`` — written by ``verify.verify_site``: the full §5 verdict. Read
#     back as the cache hit for an unchanged source, and summarised (counts only) by
#     ``/sites/by-pocket/{id}/status``.
#   * a ``pending`` verdict — the in-flight marker ``/status`` reports as ``pending``.
#     It carries its start time and goes stale after :data:`PENDING_STALE_SECONDS`, so a
#     crashed verify cannot pin a site in "pending" forever.
#
# THE BACKEND FOLLOWS THE ARTIFACT STORE'S. The worker writes and the web process reads,
# which is already true of preview artifacts, so this uses the same selection:
# ``PAW_SITES_ARTIFACT_STORE=s3`` → the shared ``StorageAdapter`` under
# ``site-verify/<pocket>/<key>.json``; otherwise the filesystem under
# ``artifact_home()/<pocket>/verify/<key>.json``. A SUBDIRECTORY on purpose: the artifact
# store's eviction globs ``<pocket>/*.json`` and would otherwise delete artifacts to make
# room for records (and records for artifacts).
#
# Best-effort on both sides, like the artifact store: a miss, a corrupt file, a dead
# bucket all read as ``None``; a failed write is logged. A lost record costs a re-verify,
# never a wrong answer — nothing here can turn into ``passed``.
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Records kept per pocket on the filesystem backend (newest by mtime).
KEEP_PER_POCKET = 8

#: A ``pending`` verdict older than this reads as absent.
PENDING_STALE_SECONDS = 900

VERIFY_KEY_PREFIX = "site-verify"

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,200}$")


def sandbox_key(content_hash: str) -> str:
    return f"sandbox-{content_hash}"


def verdict_key(content_hash: str) -> str:
    return f"verdict-{content_hash}"


def _safe(segment: str) -> bool:
    return bool(_SAFE_SEGMENT_RE.match(segment)) and segment not in (".", "..")


class FilesystemVerifyStore:
    """``artifact_home()/<pocket_id>/verify/<key>.json`` — atomic writes, bounded."""

    def _dir(self, pocket_id: str) -> Path:
        from pocketpaw_ee.sites.generator_client import artifact_home

        return artifact_home() / pocket_id / "verify"

    def read(self, pocket_id: str, key: str) -> dict[str, Any] | None:
        if not (_safe(pocket_id) and _safe(key)):
            return None
        try:
            data = json.loads((self._dir(pocket_id) / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write(self, pocket_id: str, key: str, record: dict[str, Any]) -> None:
        if not (_safe(pocket_id) and _safe(key)):
            return
        directory = self._dir(pocket_id)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(record, fh)
                os.replace(tmp, str(directory / f"{key}.json"))
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            logger.warning("sites.verify_store: write failed for %s", pocket_id, exc_info=True)
            return
        self._evict(directory)

    def _evict(self, directory: Path) -> None:
        try:
            files = sorted(
                (p for p in directory.glob("*.json") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for stale in files[KEEP_PER_POCKET:]:
            try:
                stale.unlink()
            except OSError:
                pass


class BlobVerifyStore:
    """The same records through the shared ``StorageAdapter`` (``PAW_SITES_ARTIFACT_STORE=s3``)."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def _key(self, pocket_id: str, key: str) -> str:
        return f"{VERIFY_KEY_PREFIX}/{pocket_id}/{key}.json"

    def read(self, pocket_id: str, key: str) -> dict[str, Any] | None:
        from pocketpaw_ee.sites.artifact_store_s3 import _read_all, _run_coro, _timeout_sec

        if not (_safe(pocket_id) and _safe(key)):
            return None
        try:
            raw = _run_coro(_read_all(self._adapter.open(self._key(pocket_id, key))), _timeout_sec())
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 — a miss is the common case
            return None
        return data if isinstance(data, dict) else None

    def write(self, pocket_id: str, key: str, record: dict[str, Any]) -> None:
        from pocketpaw_ee.sites.artifact_store_s3 import _bytes_stream, _run_coro, _timeout_sec

        if not (_safe(pocket_id) and _safe(key)):
            return
        payload = json.dumps(record).encode("utf-8")
        try:
            _run_coro(
                self._adapter.put(
                    self._key(pocket_id, key), _bytes_stream(payload), "application/json"
                ),
                _timeout_sec(),
            )
        except Exception:  # noqa: BLE001 — best-effort
            logger.warning("sites.verify_store: blob write failed for %s", pocket_id, exc_info=True)


_DEFAULT_STORE = FilesystemVerifyStore()


def default_verify_store() -> Any:
    """The process's verification store — blob when the artifact store is, else disk."""
    from pocketpaw_ee.sites import artifact_store_s3

    if artifact_store_s3.artifact_store_mode() == "s3":
        adapter = artifact_store_s3._shared_adapter()
        if adapter is not None:
            return BlobVerifyStore(adapter)
    return _DEFAULT_STORE


__all__ = [
    "KEEP_PER_POCKET",
    "PENDING_STALE_SECONDS",
    "BlobVerifyStore",
    "FilesystemVerifyStore",
    "default_verify_store",
    "sandbox_key",
    "verdict_key",
]
