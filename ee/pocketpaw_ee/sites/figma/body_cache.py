# ee/pocketpaw_ee/sites/figma/body_cache.py: Figma answers kept on the private store.
#
# ``RestFigmaSource`` keeps every tier-1 answer it paid for (node trees, the page
# index, render bytes, image-fill bytes, local variables) here, so a later tool call,
# a later session or another member importing the same file version costs no tier-1
# read. One folder per file version:
#
#   sites-figma-cache/{workspace}/{file_key}/{version}/{name}
#
# with flat entry names (``nodes~1-2.json``, ``render-png~1-2.bin``, ...) and an
# ``_at.json`` marker written with the first entry.
#
# Invariants a reader must not break:
#   * the version is the ``version`` from the CALLER's own ``/files/:key/meta`` call
#     (tier 3), never ``last_touched_at``; nothing here is read before that call
#     succeeded, so the cache is shared across members only once Figma confirmed the
#     caller may open the file. This module never decides access;
#   * every key part is checked against a strict pattern, so a key never leaves its
#     workspace's folder;
#   * a version folder older than ``TTL_SECONDS`` reads as empty and is deleted;
#     writing a new version deletes the file's older version folders;
#   * the store is the PRIVATE upload adapter (``site_references._adapter``): these
#     bodies are agent input, never published. A store failure is a cache miss.

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)

PREFIX = "sites-figma-cache"
TTL_SECONDS = 30 * 24 * 3600
#: Largest entry read back (an 8 MB nodes answer or a 10 MB render, with room).
MAX_ENTRY_BYTES = 12 * 1024 * 1024
_PART_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_VERSION_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_NAME_RE = re.compile(r"[A-Za-z0-9_.~-]{1,120}")
_MARKER = "_at.json"


def _store() -> Any:
    from pocketpaw_ee.sites import site_references

    return site_references._adapter()


def safe_id(node_id: str) -> str:
    """A validated node id as a key part: ``I1:2;3:4`` -> ``I1-2_3-4``."""
    return node_id.replace(":", "-").replace(";", "_")


class Bodies:
    """One file version's entries. Build with :func:`for_version`."""

    def __init__(self, store: Any, workspace: str, key: str, version: str) -> None:
        self._store = store
        self._file = f"{PREFIX}/{workspace}/{key}/"
        self._version = version
        self.prefix = f"{self._file}{version}/"
        self._alive: bool | None = None

    async def _read(self, key: str) -> bytes | None:
        buf = bytearray()
        try:
            async for chunk in self._store.open(key):
                buf.extend(chunk)
                if len(buf) > MAX_ENTRY_BYTES:
                    return None
        except Exception:  # noqa: BLE001 - missing or unreadable: a miss
            return None
        return bytes(buf) or None

    async def _write(self, key: str, data: bytes, mime: str) -> None:
        async def body() -> AsyncIterator[bytes]:
            yield data

        await self._store.put(key, body(), mime)

    async def _files(self, prefix: str) -> list[str]:
        try:
            return [i.name for i in await self._store.browse(prefix) if not i.is_dir]
        except Exception:  # noqa: BLE001
            return []

    async def _drop(self, prefix: str) -> None:
        for name in await self._files(prefix):
            try:
                await self._store.delete(prefix + name)
            except Exception:  # noqa: BLE001
                logger.debug("figma cache: delete failed", exc_info=True)

    async def alive(self) -> bool:
        if self._alive is None:
            raw = await self._read(self.prefix + _MARKER)
            at = 0.0
            try:
                at = float(json.loads(raw or b"{}").get("at") or 0)
            except (ValueError, TypeError, AttributeError):
                at = 0.0
            self._alive = bool(at) and time.time() - at < TTL_SECONDS
            if at and not self._alive:
                await self._drop(self.prefix)
        return self._alive

    async def get(self, name: str) -> bytes | None:
        if not _NAME_RE.fullmatch(name) or not await self.alive():
            return None
        return await self._read(self.prefix + name)

    async def get_json(self, name: str) -> Any:
        raw = await self.get(name)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def put(self, name: str, data: bytes, mime: str = "application/octet-stream") -> None:
        if not _NAME_RE.fullmatch(name):
            return
        try:
            if not await self.alive():
                await self._supersede()
                await self._write(
                    self.prefix + _MARKER,
                    json.dumps({"at": time.time()}).encode(),
                    "application/json",
                )
                self._alive = True
            await self._write(self.prefix + name, data, mime)
        except Exception:  # noqa: BLE001 - a lost cache write never fails a read
            logger.debug("figma cache: write failed", exc_info=True)

    async def put_json(self, name: str, value: Any) -> None:
        await self.put(name, json.dumps(value, separators=(",", ":")).encode(), "application/json")

    async def names(self, start: str) -> list[str]:
        if not await self.alive():
            return []
        return [n for n in await self._files(self.prefix) if n.startswith(start)]

    async def _supersede(self) -> None:
        """A new version of the file: its older version folders are dead weight."""
        try:
            items = await self._store.browse(self._file)
        except Exception:  # noqa: BLE001
            return
        for item in items:
            if item.is_dir and item.name != self._version:
                await self._drop(f"{self._file}{item.name}/")


def for_version(workspace: str, key: str, version: str, store: Any = None) -> Bodies | None:
    """The cache folder for one file version, or None when a part is not cacheable."""
    if not (_PART_RE.fullmatch(workspace or "") and _PART_RE.fullmatch(key or "")):
        return None
    if not _VERSION_RE.fullmatch(version or "") or version in (".", ".."):
        return None
    try:
        return Bodies(store if store is not None else _store(), workspace, key, version)
    except Exception:  # noqa: BLE001 - no store configured: no cache
        logger.debug("figma cache: no store", exc_info=True)
        return None
