# extracted_text.py — persist ONE extraction per file, so nothing re-extracts.
#
# Created 2026-08-29 (T0, "Persist the extracted text"): before this module the
# upload listener ran the extraction chain, used the text, and threw it away —
# it persisted only the COMPILED kb-go article and that article's id. Every
# later consumer therefore paid the whole chain again over the same bytes:
# ``book_agent._extract_text`` did, a transcription consumer is about to, and a
# captioning extractor bills per pass. A 500-page book is ~940K chars and tens
# of seconds of chain time, per feature, per file.
#
# WHAT IS STORED: the serialized ``ExtractionResult`` — text, title, captions,
# metadata, backend — not just the text. ``comprehend`` reads title + captions
# alongside text, and the book agent names the agent from ``title``; persisting
# only ``text`` would leave both of them re-extracting for the fields they were
# missing. One blob, one shape, every consumer served.
#
# WHERE IT IS STORED: a ``StorageAdapter`` blob, NOT a Mongo field. The full
# argument (listing hydration, the 16 MB BSON cap, and why a cap would defeat
# the book agent) is in ``models.py``'s module docstring beside the two columns
# this module writes. The key is DETERMINISTIC on ``file_id`` so a re-ingest
# overwrites in place rather than leaking one object per pass.
#
# NO SIZE CAP, deliberately. The blob has no BSON limit to respect, the text is
# already bounded by a file the deployment agreed to store, and the consumer
# this exists for (a co-reader agent) needs the WHOLE book. Truncating here
# would reintroduce exactly the re-extraction we are removing, only worse —
# silently, and only for the largest files.
#
# THE TWO CONTRACTS, and they are not symmetric:
#   * ``persist_extracted_text`` fails OPEN and returns ``False``. It runs after
#     a successful extraction and before comprehension and the KB ingest; if it
#     could raise, a storage hiccup would cost the user the ingest they actually
#     asked for. The price of failing open is that consumers re-extract — the
#     status quo, not a regression. It is logged at WARNING because a PERMANENT
#     failure here is invisible from outside (everything still works, just
#     slowly and expensively forever), which is the failure mode this codebase
#     keeps getting bitten by.
#   * ``load_extracted_text`` fails CLOSED to ``None`` on every doubt: no
#     pointer, a hidden file, a stale version, an unreachable adapter, a missing
#     or corrupt blob. ``None`` means "re-extract", which is always correct and
#     never wrong — so every refusal is safe, and no caller needs to know which
#     one fired.
#
# THE HIDE GATE LIVES HERE. ``hide_from_ai`` is checked on the READ, not just at
# the call sites, because this is a NEW door onto file content and the codebase
# fail-closes privacy at every door (the upload listener returns early, the book
# agent raises Forbidden). The third consumer will not remember to gate; putting
# the check on the door means it cannot forget. This is not a second copy of the
# listener's rule — the listener gates whether to PRODUCE text, this gates
# whether to SERVE it.

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult

logger = logging.getLogger(__name__)

# Derived artifacts live under their own prefix so a storage browse never
# confuses them with a user's uploaded blobs, and so a future sweeper can find
# every one of them with a single prefix listing.
_KEY_PREFIX = "derived/extraction"

_BLOB_MIME = "application/json; charset=utf-8"


def blob_key(file_id: str) -> str:
    """The deterministic storage key for one file's extraction blob.

    Deterministic on ``file_id`` so a re-ingest OVERWRITES: a random key per
    pass would leak one dead object per upload event, and a blob written while
    the column write failed would be unreachable forever instead of simply
    replaced on the next pass.

    Raises ``ValueError`` for a ``file_id`` that could escape the prefix. Ids
    are uuid4 hex in every writer today, so this is a guard against a future
    caller inventing its own id, not a live threat — but a key is a path on the
    local adapter, and a path built from an id is a traversal if nobody looks.
    """
    fid = (file_id or "").strip()
    if not fid:
        raise ValueError("blob_key: file_id is required")
    if "/" in fid or "\\" in fid or ".." in fid or any(ord(c) < 0x20 for c in fid):
        raise ValueError(f"blob_key: unsafe file_id {file_id!r}")
    return f"{_KEY_PREFIX}/{fid}.json"


def _resolve_adapter() -> Any | None:
    """The EE upload singleton's storage adapter, or ``None``.

    Imported inside the function for the same reason ``listeners`` and
    ``book_agent`` do it: the router owns the singleton, so an import-time
    dependency here would freeze the adapter for tests that monkeypatch it.
    """
    try:
        from pocketpaw_ee.cloud.uploads.router import _ADAPTER

        return _ADAPTER
    except Exception:
        logger.exception("extracted-text: upload adapter import failed")
        return None


async def persist_extracted_text(
    *,
    file_id: str,
    workspace_id: str,
    result: ExtractionResult,
    content_version: int,
    adapter: Any | None = None,
) -> bool:
    """Store ``result`` as this file's extraction blob. ``True`` when it landed.

    ``content_version`` must be the version read BEFORE the extraction ran.
    That ordering is the point: if an inline edit bumps the row mid-extraction,
    we record the OLD version, the reader sees a mismatch and re-extracts.
    Recording the version afterwards would stamp text extracted from old bytes
    as current — stale text served as fresh, which is worse than no text.

    Fails open: every failure returns ``False`` after logging, so the caller's
    comprehension and KB ingest continue untouched.
    """
    if not file_id or not workspace_id:
        logger.warning(
            "extracted-text: refusing to persist without file_id/workspace "
            "(file_id=%r workspace=%r)",
            file_id,
            workspace_id,
        )
        return False

    try:
        payload = result.model_dump_json().encode("utf-8")
    except Exception:
        logger.warning(
            "extracted-text: could not serialize the extraction for file_id=%s; "
            "every later consumer will re-extract this file",
            file_id,
            exc_info=True,
        )
        return False

    store_adapter = adapter if adapter is not None else _resolve_adapter()
    if store_adapter is None:
        logger.warning(
            "extracted-text: no storage adapter; file_id=%s keeps re-extracting",
            file_id,
        )
        return False

    try:
        key = blob_key(file_id)
    except ValueError:
        logger.warning("extracted-text: unusable key for file_id=%r", file_id, exc_info=True)
        return False

    async def _body() -> AsyncIterator[bytes]:
        yield payload

    try:
        await store_adapter.put(key, _body(), _BLOB_MIME)
    except Exception:
        logger.warning(
            "extracted-text: blob write failed for file_id=%s (key=%s); the "
            "ingest is unaffected but every later consumer re-extracts",
            file_id,
            key,
            exc_info=True,
        )
        return False

    # The pointer is written LAST and through the store, which stays the only
    # owner of FileUpload writes in this package. A blob with no pointer is
    # inert (the reader needs the column) and is overwritten on the next pass,
    # so the failure ordering here leaks nothing that survives a re-ingest.
    try:
        from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

        updated = await MongoFileStore().set_extracted_text(
            file_id,
            workspace_id,
            key=key,
            content_version=content_version,
        )
    except Exception:
        logger.warning(
            "extracted-text: pointer write failed for file_id=%s; the blob is "
            "stored but unreachable, so consumers re-extract until the next ingest",
            file_id,
            exc_info=True,
        )
        return False

    if updated is None:
        logger.warning(
            "extracted-text: no live row for file_id=%s workspace=%s at pointer-write time",
            file_id,
            workspace_id,
        )
        return False

    logger.info(
        "extracted-text: persisted %d chars for file_id=%s at content_version=%d",
        len(result.text or ""),
        file_id,
        content_version,
    )
    return True


async def load_extracted_text(
    doc: Any,
    *,
    adapter: Any | None = None,
) -> ExtractionResult | None:
    """Return the persisted extraction for ``doc``, or ``None`` to re-extract.

    ``None`` is returned — never raised — for every one of:

    * no ``doc``, or no ``extracted_text_key`` (legacy row, or a persist that
      failed);
    * ``hide_from_ai`` is set. The privacy gate lives on this door, not only at
      the call sites (see the module header);
    * ``extracted_text_version`` does not equal the row's current
      ``content_version``. ``cloud/file_versions`` rewrites bytes and bumps that
      counter WITHOUT emitting ``FileReady``, so an edited file's stored text is
      text of a document that no longer exists. Stale is treated as absent;
    * the adapter is unreachable, the blob is gone, or the JSON does not parse
      into an ``ExtractionResult``.

    Every refusal means "extract it yourself", which is what the caller did
    before this module existed — so a caller can act on ``None`` without
    knowing which refusal fired.
    """
    if doc is None:
        return None

    key = getattr(doc, "extracted_text_key", None)
    if not key:
        return None

    file_id = getattr(doc, "file_id", "?")

    if getattr(doc, "hide_from_ai", False):
        logger.info(
            "extracted-text: file_id=%s is hidden from AI; refusing to serve "
            "its stored text",
            file_id,
        )
        return None

    stored_version = getattr(doc, "extracted_text_version", None)
    current_version = getattr(doc, "content_version", 0) or 0
    if stored_version != current_version:
        logger.info(
            "extracted-text: file_id=%s stored text is from content_version=%r "
            "but the file is at %r; re-extracting",
            file_id,
            stored_version,
            current_version,
        )
        return None

    store_adapter = adapter if adapter is not None else _resolve_adapter()
    if store_adapter is None:
        logger.warning(
            "extracted-text: no storage adapter; file_id=%s falls back to re-extraction",
            file_id,
        )
        return None

    try:
        chunks: list[bytes] = []
        async for chunk in store_adapter.open(key):
            chunks.append(chunk)
        raw = b"".join(chunks)
    except Exception:
        logger.warning(
            "extracted-text: blob read failed for file_id=%s (key=%s); re-extracting",
            file_id,
            key,
            exc_info=True,
        )
        return None

    try:
        return ExtractionResult.model_validate_json(raw)
    except Exception:
        logger.warning(
            "extracted-text: stored blob for file_id=%s did not parse as an "
            "ExtractionResult; re-extracting",
            file_id,
            exc_info=True,
        )
        return None


async def delete_extracted_text(file_id: str, *, adapter: Any | None = None) -> None:
    """Best-effort removal of one file's extraction blob.

    Called from the upload delete path so a deleted file does not leave its
    extracted text at rest. Swallows every failure: the row is already
    tombstoned by then, and an orphaned derived blob is a cleanup-job problem,
    not a reason to fail a delete the user asked for.
    """
    if not file_id:
        return
    store_adapter = adapter if adapter is not None else _resolve_adapter()
    if store_adapter is None:
        return
    try:
        await store_adapter.delete(blob_key(file_id))
    except Exception:
        logger.warning(
            "extracted-text: could not delete the extraction blob for file_id=%s; "
            "the file is deleted, the derived blob is an orphan",
            file_id,
            exc_info=True,
        )


__all__ = [
    "blob_key",
    "delete_extracted_text",
    "load_extracted_text",
    "persist_extracted_text",
]
