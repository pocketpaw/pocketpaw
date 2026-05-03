# listeners.py — In-process subscribers for upload-related bus events.
# Created: 2026-04-30 — Stage 1.B of "Files as Knowledge". Wires FileReady
#   into the extraction chain and ingests the resulting text into the
#   workspace KB scope. Pocket-scope routing lands in Stage 3.E.
# Updated: 2026-04-30 evening — Stage 1.B follow-up. Remote storage
#   adapters (S3, GCS) don't expose a local path; the listener now streams
#   the blob into a NamedTemporaryFile via the adapter's async open() and
#   runs extraction on the temp file, cleaning up afterwards. Local-disk
#   adapters keep using the direct path with no extra I/O.
"""Upload bus subscribers.

The upload pipeline emits :class:`FileReady` on every successful upload.
This module subscribes that event and runs the indexing flow:

  1. Resolve a Path the extractor can read — either the adapter's local
     path (local-disk deployments) or a temp file streamed from the
     adapter's ``open()`` (S3, GCS, any remote adapter).
  2. Run the configured extraction chain to produce searchable text.
  3. Ingest the text into the kb-go scope ``workspace:{wid}``.
  4. Clean up the temp file on the way out, regardless of success.

Failures are isolated — a broken extraction or a missing kb binary must not
propagate back to the upload publisher. The bus already wraps each handler
in a try/except, but we keep the listener defensive so the failure mode is
"file uploads, but doesn't auto-index" rather than "upload aborts".

Pocket-scope routing arrives in Stage 3.E: the listener will check
``event.data.get("pocket_id")`` and route into ``pocket:{id}`` when set.
"""

from __future__ import annotations

import contextlib
import logging
import mimetypes
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from ee.cloud._core.realtime.bus import get_bus
from ee.cloud._core.realtime.events import Event, FileReady

logger = logging.getLogger(__name__)


async def index_uploaded_file(event: Event) -> None:
    """Resolve the file, extract via the chain, ingest into workspace KB.

    The signature accepts the base ``Event`` to satisfy the bus's
    ``Handler`` protocol. We only ever subscribe this to ``file.ready`` so
    the runtime type is always :class:`FileReady` — but typing it loosely
    here keeps mypy happy without an ``# type: ignore`` at the bus
    registration site.
    """
    data = event.data or {}
    workspace_id = data.get("workspace_id") or data.get("workspace")
    file_id = data.get("file_id")
    filename = data.get("filename") or "upload"
    mime = data.get("mime") or "application/octet-stream"
    storage_key = data.get("storage_key")

    if not workspace_id or not file_id:
        logger.debug(
            "FileReady missing workspace_id or file_id; skipping index "
            "(workspace_id=%r, file_id=%r)",
            workspace_id,
            file_id,
        )
        return

    async with _path_for_extraction(storage_key, mime, filename) as path:
        if path is None:
            logger.info(
                "skipping KB index: no path for file_id=%s storage_key=%r",
                file_id,
                storage_key,
            )
            return

        try:
            from ee.cloud.extraction import build_chain
            from pocketpaw.config import get_settings

            chain = build_chain(get_settings())
            result = await chain.run(path, mime)
        except Exception:
            logger.exception("extraction failed for file_id=%s", file_id)
            return

        text = (result.text or "").strip()
        if not text:
            logger.info(
                "extracted empty text for file_id=%s; skipping KB ingest",
                file_id,
            )
            return

        try:
            from ee.cloud.agents.knowledge import KnowledgeService

            await KnowledgeService.ingest_text_to_scope(
                scope=f"workspace:{workspace_id}",
                text=text,
                source=filename,
            )
        except Exception:
            logger.exception("KB ingest failed for file_id=%s", file_id)


@contextlib.asynccontextmanager
async def _path_for_extraction(
    storage_key: str | None,
    mime: str,
    filename: str,
) -> AsyncIterator[Path | None]:
    """Yield a Path the extraction chain can read.

    Local-disk adapter: yields the on-disk path unchanged (zero extra I/O).
    Remote adapter (S3, GCS): streams the blob into a NamedTemporaryFile,
    yields its Path, then deletes the file on exit. The temp suffix
    matches the original filename so suffix-routed extractors (pypdf for
    .pdf, python-docx for .docx, etc.) keep working.

    Yields ``None`` when neither path works — the listener treats that as
    "skip this upload, log, and move on".
    """
    if not storage_key:
        yield None
        return

    direct = _resolve_local_path(storage_key)
    if direct is not None:
        yield direct
        return

    adapter = _resolve_adapter()
    if adapter is None:
        yield None
        return

    suffix = _extension_for(filename, mime)
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 — manual lifecycle
        prefix="paw-extract-",
        suffix=suffix,
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        try:
            async for chunk in adapter.open(storage_key):
                tmp.write(chunk)
        except Exception:
            logger.exception("stream-to-temp failed for storage_key=%r", storage_key)
            yield None
            return
        finally:
            tmp.close()

        yield tmp_path
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("temp cleanup failed for %s", tmp_path)


def _resolve_local_path(storage_key: str) -> Path | None:
    """Return the adapter's on-disk path for ``storage_key`` if available."""
    adapter = _resolve_adapter()
    if adapter is None:
        return None
    try:
        return adapter.local_path(storage_key)
    except Exception:
        logger.exception("local_path lookup failed for storage_key=%r", storage_key)
        return None


def _resolve_adapter():
    """Look up the EE upload singleton's storage adapter.

    Returns ``None`` when the upload router hasn't been mounted (test
    contexts without the cloud surface). Importing inside the function so
    test harnesses can monkeypatch ``_ADAPTER`` between sub-tests without
    hitting an import-time freeze.
    """
    try:
        from ee.cloud.uploads.router import _ADAPTER

        return _ADAPTER
    except Exception:
        logger.exception("upload adapter import failed")
        return None


def _extension_for(filename: str, mime: str) -> str:
    """Pick a temp-file suffix the extractors will route correctly.

    Prefers the original filename's extension (matches what the user
    uploaded). Falls back to ``mimetypes.guess_extension`` and finally to
    an empty string when the MIME isn't registered. Suffix matters because
    ``LocalExtractor`` routes by ``path.suffix`` (pypdf for .pdf, docx for
    .docx, pytesseract for .png/.jpg) and ``GeminiFlashExtractor`` checks
    MIME directly so extension is forgiven there.
    """
    suffix = Path(filename).suffix
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(mime, strict=False) or ""
    return guessed


def register_upload_listeners() -> None:
    """Wire the upload subscribers into the bus.

    Called once during ``mount_cloud`` after ``init_realtime`` has installed
    the singleton bus. Idempotent only at the framework level — calling
    twice would register the same handler twice. The bootstrap path calls
    it exactly once.
    """
    bus = get_bus()
    bus.subscribe(FileReady.EVENT_TYPE, index_uploaded_file)


__all__ = ["index_uploaded_file", "register_upload_listeners"]
