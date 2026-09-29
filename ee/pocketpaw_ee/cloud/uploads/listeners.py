# listeners.py — In-process subscribers for upload-related bus events.
# `file.ready` is subscribed by `schedule_index_uploaded_file`, which only
# spawns a background task and returns: `bus.publish` awaits handlers inline,
# so running the pipeline in the handler held every upload request for the
# whole index (10-60 s per PDF, up to 10 min for media). Tasks are held in a
# strong-ref set, bounded by `_INDEX_SLOTS`, and their errors are logged.
# `drain_pending_indexing` finishes (then cancels) them at shutdown. Work in
# flight is lost on a crash; the durable fix is moving this to the arq worker.
#
# `index_uploaded_file` is the pipeline itself, in order:
#   hide_from_ai gate (fail CLOSED: an unresolvable row is never indexed) ->
#   materialize the blob to a local path -> extract (audio/video go to
#   `uploads.transcription` instead of the chain) -> persist the whole
#   ExtractionResult (fail OPEN) -> note links/tags + derived auto-tags ->
#   comprehension summary (daily cap claimed first, fails CLOSED; a human
#   summary is never overwritten) -> kb-go ingest into `pocket:{id}` or
#   `workspace:{wid}` -> record `kb_article_id` (None means "not indexed", so
#   it is the index status the client sees) -> optional vector ingest.
# Every step after extraction is contained: a failure there logs and leaves
# the steps that already succeeded in place. Tag and comprehension writes emit
# `file.updated` so /files refetches the row. A re-index that lands under a new
# article id removes the old article first: one file, one article.
"""Upload bus subscribers.

The upload pipeline emits :class:`FileReady` on every successful upload.
``schedule_index_uploaded_file`` hands it to a bounded background task running
``index_uploaded_file``, so the upload request never waits on indexing and an
indexing failure can only ever mean "file uploads, but doesn't auto-index".
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pocketpaw_ee.cloud._core.realtime.bus import get_bus
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import Event, FileReady
from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult
from pocketpaw_ee.cloud.uploads.extracted_text import persist_extracted_text
from pocketpaw_ee.cloud.uploads.resolver import materialize_to_local_path
from pocketpaw_ee.cloud.uploads.transcription import is_transcribable, transcribe_media

logger = logging.getLogger(__name__)

# ponytail: in-process and bounded per web process; the arq worker is the
# durable, horizontally scaled home for this pipeline.
_INDEX_CONCURRENCY = 4
_INDEX_SLOTS = asyncio.Semaphore(_INDEX_CONCURRENCY)
_pending_index_tasks: set[asyncio.Task] = set()


async def schedule_index_uploaded_file(event: Event) -> None:
    """``file.ready`` subscriber: start indexing in the background and return.

    The bus awaits this inline inside the upload request, so it must not do
    the work itself.
    """
    file_id = (event.data or {}).get("file_id")
    task = asyncio.create_task(_index_in_background(event), name=f"index-upload:{file_id}")
    _pending_index_tasks.add(task)
    task.add_done_callback(_pending_index_tasks.discard)


async def _index_in_background(event: Event) -> None:
    async with _INDEX_SLOTS:
        try:
            await index_uploaded_file(event)
        except Exception:
            logger.exception(
                "background index failed for file_id=%s", (event.data or {}).get("file_id")
            )


async def drain_pending_indexing(timeout: float = 30.0) -> int:
    """Wait up to ``timeout`` s for in-flight indexing, then cancel the rest.

    Returns how many tasks were cancelled. Meant for the cloud shutdown hooks.
    """
    pending = set(_pending_index_tasks)
    if not pending:
        return 0
    _, not_done = await asyncio.wait(pending, timeout=timeout)
    for task in not_done:
        task.cancel()
    if not_done:
        await asyncio.gather(*not_done, return_exceptions=True)
        logger.warning("cancelled %d upload index task(s) at shutdown", len(not_done))
    return len(not_done)


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
            "file_id=%s is hidden from AI (hide_from_ai=True); skipping KB index and auto-tagging",
            file_id,
        )
        return
    existing_tags = list(getattr(doc, "tags", []) or [])
    # FC-3: read the library state the comprehension pass has to respect while
    # we still hold the row — the shelves it merges into, and the summary it
    # must not clobber if a person already wrote one.
    existing_collections = list(getattr(doc, "collections", []) or [])
    existing_summary = getattr(doc, "summary", None)
    # T0: the version the extraction we are about to run describes. Read BEFORE
    # the chain runs, on purpose — if an inline edit bumps it while extraction
    # is in flight, we stamp the OLD version, the reader sees a mismatch and
    # re-extracts. Reading it afterwards would label text from stale bytes as
    # current, which is the one outcome worse than not persisting at all.
    content_version = getattr(doc, "content_version", 0) or 0

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

        # T2: a recording is transcribed, not extracted — and the branch is
        # exclusive on purpose. ``LocalExtractor`` claims every mime
        # (``supports_mimes = {"*"}``) and its last branch is
        # ``path.read_text(errors="replace")``, so sending a video through the
        # chain slurps the whole binary into a string of replacement
        # characters, which then gets persisted, summarised, tagged and pushed
        # into the knowledge base. Media goes to ``transcription`` instead, and
        # comes back as an ordinary ``ExtractionResult`` — so everything below
        # this point (persist, tags, comprehension, KB ingest) treats a podcast
        # exactly like a PDF, with no new code in any of those paths.
        if is_transcribable(mime):
            result = await transcribe_media(
                path=path,
                mime=mime,
                file_id=file_id,
                workspace_id=str(workspace_id),
                filename=filename,
            )
            if result is None:
                # Nothing was learned about the FILE — only that transcription
                # was unavailable (no key, today's budget spent, fal errored).
                # Persist nothing, so the next ingest is a clean retry.
                logger.info(
                    "file_id=%s (%s): no transcript this pass; leaving the file "
                    "un-indexed rather than recording a result we do not have",
                    file_id,
                    mime,
                )
                return
        else:
            try:
                from pocketpaw.config import get_settings
                from pocketpaw_ee.cloud.extraction import build_chain

                chain = build_chain(get_settings())
                result = await chain.run(path, mime)
            except Exception:
                logger.exception("extraction failed for file_id=%s", file_id)
                return

        # T0: persist the extraction ONCE, here, before anything consumes it.
        # This is the only place in the system that runs the chain on upload,
        # so it is the only place that can hand the result to everyone else —
        # the book agent and (next) transcription both re-ran the whole chain
        # over the same bytes because this line did not exist.
        #
        # Contained and fail-OPEN by contract (``persist_extracted_text``
        # returns False rather than raising), so a storage failure costs a
        # future re-extraction and nothing else: comprehension, auto-tagging
        # and the KB ingest below all proceed exactly as before.
        await persist_extracted_text(
            file_id=file_id,
            workspace_id=str(workspace_id),
            result=result,
            content_version=content_version,
            adapter=adapter,
        )

        # FL-6: auto-tag from extraction output. Independent of KB ingest —
        # runs before it so a file still gets tags even if the KB write later
        # fails. Contained: a tag-write error must not abort indexing.
        await _write_auto_tags(
            file_id=file_id,
            workspace_id=str(workspace_id),
            result=result,
            existing_tags=existing_tags,
            note=_parse_note(mime, getattr(result, "text", None), file_id=file_id),
            existing_links=list(getattr(doc, "link_names", []) or []),
        )

        # FC-3: comprehension, beside auto-tagging and independent of it. Runs
        # before the KB ingest for the same reason FL-6 does — a file should
        # still be understood when the KB write later fails — and is contained
        # the same way, so nothing here can abort the ingest.
        await _write_comprehension(
            file_id=file_id,
            workspace_id=str(workspace_id),
            extracted=result,
            mime=mime,
            existing_collections=existing_collections,
            existing_summary=existing_summary,
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
            previous=(getattr(doc, "kb_article_id", None), getattr(doc, "kb_scope", None)),
        )

        await _maybe_attach_vector(
            path=path,
            mime=mime,
            article_id=article_id,
            scope=scope,
            file_id=file_id,
        )


def _extract_article_id(ingest_result) -> str | None:
    """Pull the article id out of a kb-go ingest receipt.

    Delegates to ``knowledge.extract_ingest_article_id`` — kb-go's actual
    receipt keys the id as ``article`` (finishIngest), which the old inline
    ``id``/``article_id`` lookup here never matched, so the FL-11b tracking
    write (and the vector path) silently never ran for real receipts.
    """
    from pocketpaw_ee.cloud.agents.knowledge import extract_ingest_article_id

    return extract_ingest_article_id(ingest_result)


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
    previous: tuple[str | None, str | None] = (None, None),
) -> None:
    """Persist the kb-go article id + scope on the FileUpload row (FL-11b).

    Lets a later hide-from-AI toggle purge exactly this article from the KB.
    ``previous`` is the (article_id, scope) the row tracked before this
    ingest; when the fresh article landed under a different id the old one is
    removed so a re-indexed note never leaves two articles behind.
    Fully contained: any failure (store unavailable, row already gone) is
    logged and swallowed so the ingest that already succeeded is never undone.
    """
    try:
        old_id, old_scope = previous
        if old_id and old_scope and old_id != article_id:
            from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

            # remove_article swallows its own subprocess errors and answers
            # False. Ignoring that answer strands the old article: nothing
            # tracks it any more, it keeps answering searches, and a later
            # hide-from-AI purge only ever targets the new id. Say so loudly
            # instead — the ingest still stands, but the leftover is named.
            if not await KnowledgeService.remove_article(old_scope, old_id):
                logger.error(
                    "kb article %s in scope %s could not be removed while "
                    "re-indexing file_id=%s; it is now orphaned and will not "
                    "be purged by a later hide_from_ai toggle",
                    old_id,
                    old_scope,
                    file_id,
                )

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
    note=None,
    existing_links: list[str] | None = None,
) -> None:
    """Derive free-form tags from extraction output and persist the union.

    Reuses whatever extraction already produced (title, captions, text, and
    any adapter-supplied labels in ``metadata``) — never calls a new external
    LLM. Merges with ``existing_tags`` so a user-applied tag survives a
    re-index. ``note`` (a parsed ``NoteLinks``, text notes only) contributes
    its ``#hashtags`` + frontmatter tags AHEAD of the derived keywords, so a
    tag the author typed is never the one dropped at the cap, and its
    ``link_names`` land in the same write. Fully contained: any failure (or
    an empty derivation, or a missing row) leaves the file untagged rather
    than aborting the ingest.
    """
    try:
        from pocketpaw_ee.cloud.uploads.tagging import derive_tags, merge_tags

        derived = derive_tags(
            title=getattr(result, "title", None),
            captions=getattr(result, "captions", None),
            text=getattr(result, "text", None),
            metadata=getattr(result, "metadata", None),
        )
        base = list(existing_tags)
        link_names: list[str] | None = None
        if note is not None:
            # A typed #tag skips the keyword noise floor: #q3 and #ai are real tags.
            base = merge_tags(base, [*note.hashtags, *note.frontmatter_tags], min_len=2)
            link_names = list(note.link_names)
        merged = merge_tags(base, derived)
        # Nothing new to write (derivation empty and no existing tags to
        # normalize into place) — skip the DB round-trip.
        if merged == list(existing_tags) and (
            link_names is None or link_names == list(existing_links or [])
        ):
            return

        from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

        updated = await MongoFileStore().set_library_metadata(
            file_id, workspace_id, tags=merged, link_names=link_names
        )
        if updated is None:
            logger.debug(
                "auto-tag write found no row for file_id=%s workspace=%s",
                file_id,
                workspace_id,
            )
        else:
            logger.info("auto-tagged file_id=%s with %d tag(s)", file_id, len(merged))
            # Tell the Library the row changed; file.ready fired before this
            # write, so without it the tag pane is stale until a reload.
            await emit(
                Event(
                    type="file.updated",
                    data={"file_id": file_id, "workspace_id": workspace_id, "reason": "tags"},
                )
            )
    except Exception:
        logger.exception("auto-tagging failed for file_id=%s; KB ingest unaffected", file_id)


async def _write_comprehension(
    *,
    file_id: str,
    workspace_id: str,
    extracted: ExtractionResult,
    mime: str,
    existing_collections: list[str],
    existing_summary: str | None,
) -> None:
    """Ask a model what this file IS and persist the summary + collections.

    T0 note — WHY THIS DOES NOT READ THE STORED TEXT BACK. ``extracted`` is now
    the typed ``ExtractionResult``: the same shape ``extracted_text`` persists
    and the same shape ``load_extracted_text`` returns, so a backfill that wants
    to re-comprehend an old file can hand this function a loaded blob and
    nothing else changes. What it deliberately does NOT do is fetch the blob the
    caller wrote three lines earlier — that would be a storage round-trip for
    text already in memory, on the hot path of every upload. "Consumers do not
    re-extract" is the goal; re-READING what you are holding buys none of it.

    The three refusals, in the order they are cheapest to make:

    1. **A summary already written by a person stays.** The comprehension pass
       is a guess made from the first few thousand characters; a human who has
       read the file and typed a correction outranks it, every time, forever.
       We do not track provenance — any non-empty summary is treated as
       somebody's, and the cost of that simplification is that a stale machine
       summary also survives. A person can clear it (``PATCH`` with ``""``) and
       the next ingest re-writes it. ``collections`` still merges, because
       merging cannot destroy anything.
    2. **The daily cap is claimed before the call, and refuses fail-CLOSED.**
       This is the one gate in the whole path that fails closed; see
       ``comprehension_budget``'s module note for why the asymmetry is
       deliberate.
    3. **Everything after that fails OPEN.** A dead proxy, a 404 model id, a
       model that answers in prose — ``comprehend`` returns None and this
       function returns quietly. A file the user asked us to STORE must not
       fail to store because we could not describe it.

    Note what is NOT here: a ``hide_from_ai`` check. That gate lives at the top
    of ``index_uploaded_file`` and has already returned before this runs.
    Re-checking it here would create a second copy of a privacy rule, and two
    copies of a rule is one copy that can drift.
    """
    try:
        if (existing_summary or "").strip():
            logger.debug("file_id=%s already has a summary; leaving it alone", file_id)
            return

        from pocketpaw_ee.cloud.uploads import comprehension_budget

        allowed, spent, cap = await comprehension_budget.try_spend(workspace_id)
        if not allowed:
            logger.info(
                "file comprehension skipped for file_id=%s: workspace %s is at "
                "%d/%d for today (or the counter was unreadable). The file is "
                "still indexed and tagged.",
                file_id,
                workspace_id,
                spent,
                cap,
            )
            return

        from pocketpaw_ee.cloud.uploads.comprehension import comprehend
        from pocketpaw_ee.cloud.uploads.tagging import merge_tags

        understood = await comprehend(
            extracted.title,
            extracted.text,
            list(extracted.captions or []),
            mime=mime,
        )
        if understood is None:
            return

        # ``collections`` merges on the same terms tags do: existing values
        # first, order preserved, no clobber. A shelf a person put this file on
        # is not the model's to remove.
        merged = merge_tags(existing_collections, understood.categories)

        from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

        updated = await MongoFileStore().set_library_metadata(
            file_id,
            workspace_id,
            summary=understood.summary,
            collections=merged,
        )
        if updated is None:
            logger.debug(
                "comprehension write found no row for file_id=%s workspace=%s",
                file_id,
                workspace_id,
            )
        else:
            logger.info("comprehended file_id=%s into %d collection(s)", file_id, len(merged))
            await emit(
                Event(
                    type="file.updated",
                    data={
                        "file_id": file_id,
                        "workspace_id": workspace_id,
                        "reason": "comprehension",
                    },
                )
            )
    except Exception:
        logger.exception(
            "file comprehension failed for file_id=%s; the file stays indexed, tagged and usable",
            file_id,
        )


_NOTE_MIMES = frozenset({"text/markdown", "text/plain"})


def _parse_note(mime: str, text: str | None, *, file_id: str):
    """Parse links + tags out of a text note; ``None`` for anything else.

    Fail-open: a parser error logs and returns ``None`` so the file is still
    tagged and indexed like any other upload.
    """
    # Split the parameters off: an upload can arrive as
    # "text/markdown; charset=utf-8" and an exact match would silently give it
    # no links and no #tags, while the same file made in the editor (bare mime
    # from _guess_mime) worked — a difference invisible in local testing.
    if mime.split(";", 1)[0].strip().lower() not in _NOTE_MIMES or not text:
        return None
    try:
        from pocketpaw_ee.cloud.uploads.links import parse_note_links

        return parse_note_links(text)
    except Exception:
        logger.exception("note link parsing failed for file_id=%s; indexing continues", file_id)
        return None


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
    bus.subscribe(FileReady.EVENT_TYPE, schedule_index_uploaded_file)


__all__ = [
    "drain_pending_indexing",
    "index_uploaded_file",
    "register_upload_listeners",
    "schedule_index_uploaded_file",
]
