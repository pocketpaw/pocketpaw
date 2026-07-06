# listeners.py — In-process subscribers for upload-related bus events.
# Created: 2026-04-30 — Stage 1.B of "Files as Knowledge". Wires FileReady
#   into the extraction chain and ingests the resulting text into the
#   workspace KB scope. Pocket-scope routing lands in Stage 3.E.
# Updated: 2026-04-30 evening — Stage 1.B follow-up. Remote storage
#   adapters (S3, GCS) don't expose a local path; the listener now streams
#   the blob into a NamedTemporaryFile via the adapter's async open() and
#   runs extraction on the temp file, cleaning up afterwards. Local-disk
#   adapters keep using the direct path with no extra I/O.
# Updated: 2026-04-30 — Stage 2.D of "Files as Knowledge". Added the
#   vector path: after text-ingest succeeds, optionally compute an
#   embedding via the configured EmbeddingAdapter and pipe it to kb-go's
#   `kb ingest --vec <path>` surface. Cap-tracking via CostTracker keeps
#   a runaway loop from draining the budget. Vector failures are
#   contained — text-only KB still wins.
# Updated: 2026-05-03 — Stage 3.E of "Files as Knowledge". The listener
#   now reads ``pocket_id`` off the FileReady payload and routes the
#   article into ``pocket:{id}`` instead of the workspace pool. The
#   vector path inherits the same scope variable so embeddings land in
#   the same kb-go scope as the text article. Workspace uploads (no
#   ``pocket_id``) keep the previous ``workspace:{wid}`` behaviour.
# Updated: 2026-07-03 — FL-6 "Auto-tagging on ingest". The listener now
#   (1) loads the FileUpload row up front and, if ``hide_from_ai`` is set,
#   returns early — a hidden file is neither KB-indexed nor tagged (this
#   gate also lands in FL-11b; the two agree). (2) After extraction produces
#   text it derives a small set of free-form tags from title + captions +
#   text (reusing what extraction already produced — no new LLM call) via
#   ``uploads.tagging``, unions them with any pre-existing user tags, and
#   writes the result back through ``MongoFileStore.set_library_metadata``.
#   Tag derivation/write failures are contained: a broken tag write must
#   never lose the KB ingest that already succeeded.
# Updated: 2026-07-03 — FL-11b "hide-from-AI enforcement". Hardened the hide
#   gate to fail CLOSED: if the FileUpload row can't be resolved to confirm
#   ``hide_from_ai``, the listener SKIPS indexing/tagging instead of proceeding
#   (FL-6 failed open, which could index a hidden file on a metadata hiccup).
#   NOTE: purging content ALREADY indexed when a file is later hidden requires
#   a kb-go ``delete`` subcommand that does not exist yet — tracked as a
#   follow-up; this change guarantees hidden files are never indexed going
#   forward.
# Updated: 2026-07-03 — FL-11b "hide-from-AI purge" (retroactive). After a
#   successful KB ingest the listener now records the kb-go ``article_id`` and
#   the ``scope`` it landed in onto the FileUpload row (via
#   ``MongoFileStore.set_kb_article``). This lets the PATCH route retroactively
#   purge exactly that article when the file is later hidden from AI (the
#   companion delete path lives in ``uploads/router.py``). The tracking write is
#   contained — a failure logs but never undoes the ingest that succeeded.
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

import logging
from pathlib import Path

from pocketpaw_ee.cloud._core.realtime.bus import get_bus
from pocketpaw_ee.cloud._core.realtime.events import Event, FileReady
from pocketpaw_ee.cloud.uploads.resolver import materialize_to_local_path

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
    pocket_id = data.get("pocket_id")
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

    # FL-6/FL-11b: load the library row up front so we can (a) honour the
    # ``hide_from_ai`` opt-out before touching the KB and (b) union derived
    # tags with any pre-existing user tags later.
    #
    # FL-11b hardening — FAIL CLOSED on the hide gate. ``hide_from_ai`` is a
    # privacy control: if we cannot resolve the row to confirm the file is NOT
    # hidden, we must NOT index it. FL-6 previously failed *open* here (``doc``
    # is None -> proceed), which would index a genuinely hidden file whenever
    # the metadata lookup hiccupped. There's no clean signal to distinguish
    # "row genuinely absent" from "store unavailable", so any unresolvable
    # status skips indexing/tagging. A resolvable, unhidden row proceeds.
    doc = await _load_upload_doc(file_id, str(workspace_id))
    if doc is None:
        logger.info(
            "file_id=%s: could not resolve the library row to check "
            "hide_from_ai; skipping KB index and auto-tagging (fail-closed "
            "privacy gate)",
            file_id,
        )
        return
    if getattr(doc, "hide_from_ai", False):
        logger.info(
            "file_id=%s is hidden from AI (hide_from_ai=True); skipping KB "
            "index and auto-tagging",
            file_id,
        )
        return
    existing_tags = list(getattr(doc, "tags", []) or [])

    adapter = _resolve_adapter()
    if adapter is None or not storage_key:
        logger.info(
            "skipping KB index: no adapter or storage_key for file_id=%s",
            file_id,
        )
        return

    async with materialize_to_local_path(
        adapter, storage_key, mime=mime, filename=filename
    ) as path:
        if path is None:
            logger.info(
                "skipping KB index: no path for file_id=%s storage_key=%r",
                file_id,
                storage_key,
            )
            return

        try:
            from pocketpaw.config import get_settings
            from pocketpaw_ee.cloud.extraction import build_chain

            chain = build_chain(get_settings())
            result = await chain.run(path, mime)
        except Exception:
            logger.exception("extraction failed for file_id=%s", file_id)
            return

        # FL-6: auto-tag from extraction output. Independent of KB ingest —
        # runs before it so a file still gets tags even if the KB write later
        # fails. Contained: a tag-write error must not abort indexing.
        await _write_auto_tags(
            file_id=file_id,
            workspace_id=str(workspace_id),
            result=result,
            existing_tags=existing_tags,
        )

        text = (result.text or "").strip()
        if not text:
            logger.info(
                "extracted empty text for file_id=%s; skipping KB ingest",
                file_id,
            )
            return

        # Stage 3.E scope routing: pocket-scoped uploads land in
        # ``pocket:{id}``; workspace-scoped uploads keep the original
        # ``workspace:{wid}`` shape. Most-specific wins.
        if pocket_id:
            scope = f"pocket:{pocket_id}"
        else:
            scope = f"workspace:{workspace_id}"
        try:
            from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

            ingest_result = await KnowledgeService.ingest_text_to_scope(
                scope=scope,
                text=text,
                source=filename,
            )
        except Exception:
            logger.exception("KB ingest failed for file_id=%s", file_id)
            return

        article_id = _extract_article_id(ingest_result)
        if not article_id:
            logger.debug(
                "no article_id returned from kb ingest for file_id=%s; skipping vector path",
                file_id,
            )
            return

        # FL-11b: record the article id + scope on the row so a later
        # hide-from-AI toggle can purge exactly this article. Contained — a
        # tracking-write failure must not break the ingest that already
        # succeeded (worst case: the file can't be auto-purged and a sweeper
        # handles it).
        await _record_kb_article(
            file_id=file_id,
            workspace_id=str(workspace_id),
            article_id=article_id,
            scope=scope,
        )

        await _maybe_attach_vector(
            path=path,
            mime=mime,
            article_id=article_id,
            scope=scope,
            file_id=file_id,
        )


def _extract_article_id(ingest_result) -> str | None:
    """Pull the article id out of a kb-go ingest response.

    kb-go returns ``{"id": "<uuid-or-slug>"}`` on success. We handle the
    str-fallback case (kb-go falls through to raw stdout when JSON parsing
    fails — a known shape from agents/knowledge.py:_kb).
    """
    if isinstance(ingest_result, dict):
        article_id = ingest_result.get("id") or ingest_result.get("article_id")
        return article_id if isinstance(article_id, str) else None
    return None


async def _maybe_attach_vector(
    *,
    path: Path,
    mime: str,
    article_id: str,
    scope: str,
    file_id: str,
) -> None:
    """Compute an embedding and attach it to the kb-go article.

    Bails out (logs at DEBUG/INFO) when:
      - vectors are disabled in settings
      - no embedder is configured
      - the file's modality isn't supported by the configured adapter
      - the monthly cap would be exceeded by this call's pre-call estimate
      - the embed call or the kb subprocess raises (text-only KB still wins)
    """
    from pocketpaw.config import get_settings

    settings = get_settings()
    if not getattr(settings, "kb_vectors_enabled", False):
        return

    try:
        from pocketpaw_ee.cloud.embeddings import build_embedder, get_cost_tracker
    except Exception:
        # Should never happen — embeddings package imports are lazy.
        # Defensive so a packaging hiccup never crashes the listener.
        logger.exception("embeddings package import failed for file_id=%s", file_id)
        return

    embedder = build_embedder(settings)
    if embedder is None:
        return

    modality = _modality_for_mime(mime)
    if modality not in embedder.supports_modalities:
        logger.debug(
            "embedder %s does not support modality %r for mime %r; skipping",
            embedder.name,
            modality,
            mime,
        )
        return

    cost_tracker = get_cost_tracker(settings)
    estimate = embedder.estimate_cost(path, mime)
    if not cost_tracker.can_spend(estimate):
        logger.info(
            "monthly embedding cap (%.4f USD) reached; skipping vector for "
            "file_id=%s (estimated cost %.6f USD, spent so far %.6f USD)",
            cost_tracker.cap_usd,
            file_id,
            estimate,
            cost_tracker.spent_this_month,
        )
        return

    try:
        emb = await embedder.embed_file(path, mime)
    except Exception:
        logger.exception(
            "embedding failed for file_id=%s; text-only KB still ingested",
            file_id,
        )
        return

    cost_tracker.record(emb.estimated_cost_usd)

    try:
        await _write_vector_to_kb(
            article_id=article_id,
            scope=scope,
            vector=emb.vector,
        )
    except Exception:
        logger.exception(
            "kb-go vector ingest failed for file_id=%s article_id=%s; text-only KB still ingested",
            file_id,
            article_id,
        )


def _modality_for_mime(mime: str) -> str:
    """Map a MIME string to a modality name the adapter Protocol uses."""
    if mime.startswith("image/"):
        return "image"
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return "text"


async def _write_vector_to_kb(
    *,
    article_id: str,
    scope: str,
    vector: list[float],
) -> None:
    """Pipe the vector to kb-go via ``kb ingest --vec <path>``.

    kb-go's --vec flag takes a file path (not stdin), per
    kb-go/vector_cli.go:loadVectorFromFile. We write a NamedTemporaryFile
    in the ``{"vector": [...]}`` form, run the subprocess, and clean up.
    """
    import asyncio
    import json
    import os
    import tempfile

    from pocketpaw_ee.cloud.agents.knowledge import KB_BIN

    payload = json.dumps({"vector": vector})
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 — manual lifecycle
        mode="w",
        prefix="paw-vec-",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    )
    try:
        tmp.write(payload)
        tmp.flush()
        tmp.close()
        proc = await asyncio.create_subprocess_exec(
            KB_BIN,
            "ingest",
            "--vec",
            tmp.name,
            "--id",
            article_id,
            "--scope",
            scope,
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("kb ingest --vec timed out after 60s")
        if proc.returncode != 0:
            raise RuntimeError(
                f"kb ingest --vec failed (exit {proc.returncode}): "
                f"{stderr.decode('utf-8', errors='replace')[:200]}"
            )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            logger.debug("temp vec cleanup failed for %s", tmp.name)


async def _record_kb_article(
    *,
    file_id: str,
    workspace_id: str,
    article_id: str,
    scope: str,
) -> None:
    """Persist the kb-go article id + scope on the FileUpload row (FL-11b).

    Lets a later hide-from-AI toggle purge exactly this article from the KB.
    Fully contained: any failure (store unavailable, row already gone) is
    logged and swallowed so the ingest that already succeeded is never undone.
    """
    try:
        from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

        updated = await MongoFileStore().set_kb_article(
            file_id, workspace_id, article_id=article_id, scope=scope
        )
        if updated is None:
            logger.debug(
                "kb-article tracking found no row for file_id=%s workspace=%s",
                file_id,
                workspace_id,
            )
    except Exception:
        logger.exception(
            "recording kb_article_id failed for file_id=%s; KB content is "
            "ingested but won't auto-purge on hide (sweeper can reconcile)",
            file_id,
        )


async def _load_upload_doc(file_id: str, workspace_id: str):
    """Load the workspace-scoped FileUpload row, or ``None`` on any failure.

    Used for the ``hide_from_ai`` gate and to read pre-existing user tags for
    the union. Returns ``None`` when the store raises (e.g. Beanie not
    initialised) or the row is genuinely absent. FL-11b: the caller treats
    ``None`` as fail-CLOSED — indexing is skipped when the hide status can't be
    confirmed, so a hidden file is never indexed on a metadata hiccup.
    """
    try:
        from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

        return await MongoFileStore().get_doc_scoped(file_id, workspace_id)
    except Exception:
        logger.debug(
            "could not load FileUpload row for file_id=%s (store unavailable); "
            "returning None — the caller fail-closes and skips indexing",
            file_id,
        )
        return None


async def _write_auto_tags(
    *,
    file_id: str,
    workspace_id: str,
    result,
    existing_tags: list[str],
) -> None:
    """Derive free-form tags from extraction output and persist the union.

    Reuses whatever extraction already produced (title, captions, text, and
    any adapter-supplied labels in ``metadata``) — never calls a new external
    LLM. Merges with ``existing_tags`` so a user-applied tag survives a
    re-index. Fully contained: any failure (or an empty derivation, or a
    missing row) leaves the file untagged rather than aborting the ingest.
    """
    try:
        from pocketpaw_ee.cloud.uploads.tagging import derive_tags, merge_tags

        derived = derive_tags(
            title=getattr(result, "title", None),
            captions=getattr(result, "captions", None),
            text=getattr(result, "text", None),
            metadata=getattr(result, "metadata", None),
        )
        merged = merge_tags(existing_tags, derived)
        # Nothing new to write (derivation empty and no existing tags to
        # normalize into place) — skip the DB round-trip.
        if merged == list(existing_tags):
            return

        from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

        updated = await MongoFileStore().set_library_metadata(
            file_id, workspace_id, tags=merged
        )
        if updated is None:
            logger.debug(
                "auto-tag write found no row for file_id=%s workspace=%s",
                file_id,
                workspace_id,
            )
        else:
            logger.info(
                "auto-tagged file_id=%s with %d tag(s)", file_id, len(merged)
            )
    except Exception:
        logger.exception(
            "auto-tagging failed for file_id=%s; KB ingest unaffected", file_id
        )


def _resolve_adapter():
    """Look up the EE upload singleton's storage adapter.

    Returns ``None`` when the upload router hasn't been mounted (test
    contexts without the cloud surface). Importing inside the function so
    test harnesses can monkeypatch ``_ADAPTER`` between sub-tests without
    hitting an import-time freeze.
    """
    try:
        from pocketpaw_ee.cloud.uploads.router import _ADAPTER

        return _ADAPTER
    except Exception:
        logger.exception("upload adapter import failed")
        return None


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
