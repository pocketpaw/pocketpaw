# book_agent.py — provision ONE dedicated co-reader agent per uploaded book.
#
# Created 2026-08-29 (BA-2, "Make an agent of this book"): a user opens a PDF
# in /files, presses one button, and gets an agent that has READ that book and
# can talk about it. This module is the whole provisioning path.
#
# It is deliberately the same idiom as ``paw_bar.agent_provisioning.
# ensure_site_agent`` (the site-concierge provisioner) rather than a second
# one: idempotent, tenant-filtered, one dedicated agent, a deterministic slug
# as the backstop against duplicates, and an existing LIVE bind that is never
# overwritten. Read that function before changing this one.
#
# NOTHING TO WIRE ON THE CHAT SIDE. ``chat.agent_service._kb_scopes_for_context``
# already searches ``agent:{id}`` on every turn, so ingesting the book into that
# scope is the entire "it has read the book" mechanic.
#
# ORDER MATTERS — extract BEFORE create. The spec's numbered steps read
# create-then-extract, but the agent's NAME comes from the extracted title, and
# extracting first means an unreadable file leaves ZERO artifacts behind instead
# of a nameless agent nobody asked for.
#
# FAILURE POLICY (each failure is chosen, not inherited):
#   * File missing / wrong workspace  -> NotFound. The tenant filter is the
#     read itself, so a cross-tenant press is indistinguishable from a typo.
#   * ``hide_from_ai`` set            -> Forbidden. FL-11b made the upload
#     listener fail CLOSED on hidden files; ingesting the same bytes into an
#     ``agent:{id}`` scope through a different door would walk straight around
#     that privacy gate. Same gate, same answer.
#   * Extraction fails / no text      -> raise, nothing created. A co-reader
#     that has read nothing is not a degraded feature, it is a broken one.
#   * Agent creation fails            -> raise, and the file is NOT bound. The
#     feature failed; leaving a half-bound row would make the next press return
#     a dangling id.
#   * INGEST fails after the agent exists -> do NOT raise and do NOT delete the
#     agent — the user can already see it in their agent list, and deleting a
#     visible thing is worse than an honest partial. Return ``indexed=False``
#     and leave the file UNBOUND: the bind is written only on the success path,
#     so the next press retries the ingest, and the deterministic slug makes
#     that retry ADOPT the same agent instead of minting a second one. That is
#     why ``indexed`` is on the result at all — the caller has to tell the user
#     "your agent is here but it hasn't finished reading."
#
# Updated 2026-08-29 (T0, "Persist the extracted text"): the KNOWN COST that
# used to sit here is GONE. This module previously re-ran the entire extraction
# chain over bytes the upload listener had already extracted, because the
# listener persisted only the compiled kb-go article and never the raw text —
# tens of seconds and a second captioning bill for a large PDF, every press.
# The listener now stores the whole ``ExtractionResult``, so ``_extract_text``
# reads it back and only falls through to the chain when there is nothing
# stored: a file uploaded before T0, a file edited since (the stored text is
# then STALE and refused), or a persist that failed. The fallback is the old
# code path unchanged, so nothing about this feature depends on the store
# having worked.

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pocketpaw_ee.cloud._core.errors import Forbidden, Internal, NotFound, ValidationError
from pocketpaw_ee.cloud.uploads.extracted_text import load_extracted_text
from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
from pocketpaw_ee.cloud.uploads.resolver import materialize_to_local_path

logger = logging.getLogger(__name__)

# Agent.name is capped at 100 by the create DTO; truncate rather than 422.
_MAX_AGENT_NAME = 100

# The soul archetype every book agent carries. Mirrors the concierge's
# ``_CONCIERGE_ARCHETYPE`` — one archetype per provisioned agent species.
_BOOK_ARCHETYPE = "The Co-Reader"

# Separators a filename uses where a title would use a space.
_FILENAME_SEPARATORS = re.compile(r"[_\-.]+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class BookAgentResult:
    """Outcome of one ``ensure_book_agent`` call.

    ``created`` — this call minted the agent (False when an existing live bind
    was returned unchanged, or when a retry adopted an agent left behind by an
    earlier failed ingest).

    ``indexed`` — the book's text is in the agent's knowledge scope. True on a
    successful ingest AND on the idempotent early return: the bind is only ever
    written after an ingest succeeds, so a bind's existence IS the proof. False
    means the agent exists but has not read the book yet — the caller must say
    so rather than pretending it is ready.
    """

    agent_id: str
    created: bool
    indexed: bool


def book_agent_slug(file_id: str) -> str:
    """Deterministic, workspace-unique slug for a file's co-reader agent.

    Deterministic on the file id so a retry after a partial provision (agent
    created, ingest failed, bind withheld) RESOLVES the same agent by slug
    instead of minting a duplicate. This is the second half of the duplicate
    guard — the persisted ``agent_id`` bind is the first. ``book-`` + a 32-char
    uuid4 hex fits the DTO's 50-char slug cap.
    """
    return f"book-{file_id}"


def book_agent_name(title: str | None, filename: str) -> str:
    """The agent's display name: the BOOK's name, never the file's.

    A user thinks of "Thinking, Fast and Slow", not
    ``thinking-fast-and-slow_final_v2.pdf``. Prefer the title extraction
    detected; otherwise clean the filename into something a human would
    recognise — drop the extension, turn separators into spaces, collapse
    runs of whitespace. Falls back to a generic name when there is nothing
    left (a file called ``.pdf``), because ``Agent.name`` has a min_length of 1.
    """
    detected = (title or "").strip()
    if detected:
        return detected[:_MAX_AGENT_NAME]

    stem = Path(filename or "").stem
    cleaned = _WHITESPACE.sub(" ", _FILENAME_SEPARATORS.sub(" ", stem)).strip()
    return (cleaned or "This Book")[:_MAX_AGENT_NAME]


def book_agent_persona(book_name: str) -> str:
    """The soul persona seeded on the agent — a CO-READER, not an assistant.

    The distinction is the product: this agent has read one book and talks
    about that book with someone else who is reading it. Saying so in the
    persona is what keeps it from answering as a general-purpose helper that
    happens to have a document attached.
    """
    subject = (book_name or "").strip() or "this book"
    return (
        f'You are a co-reader of "{subject}". You have read it closely and you '
        "discuss it with the person reading it now — what it argues, where it "
        "goes, what a passage means, how one part answers another. Ground every "
        "answer in the book itself and quote it where that helps. When something "
        "falls outside the book, say so plainly rather than filling the gap."
    )


def _resolve_adapter() -> Any | None:
    """The EE upload singleton's storage adapter, or ``None``.

    Imported inside the function (not at module scope) for the same reason the
    upload listener does it: the router owns the singleton, so an import-time
    dependency here would invert that and freeze the adapter for tests that
    monkeypatch it.
    """
    try:
        from pocketpaw_ee.cloud.uploads.router import _ADAPTER

        return _ADAPTER
    except Exception:
        logger.exception("book agent: upload adapter import failed")
        return None


async def _extract_text(doc: Any) -> tuple[str, str | None]:
    """Return ``(text, title)`` for the file, or raise.

    T0: the upload listener already extracted this file once and now PERSISTS
    the result, so the first thing we do is read it back. On a hit the whole
    chain — and, for a scanned PDF, a second captioning bill — is skipped.

    ``load_extracted_text`` returns ``None`` for every doubt (no stored blob,
    a legacy row, a file edited since extraction, a hidden file, a corrupt
    blob), and ``None`` drops straight through to the chain below: the exact
    code that ran before T0. So the stored copy is a pure accelerator — if the
    store never worked at all, this function still behaves as it always did,
    just slowly. That is deliberate; the alternative would make a co-reader
    agent fail on a file the user can plainly read.

    A stored hit still has to clear the SAME empty-text bar as a fresh
    extraction, so an image-only scan is refused identically either way.
    """
    stored = await load_extracted_text(doc)
    if stored is not None:
        text = (stored.text or "").strip()
        if text:
            logger.info(
                "book agent: reusing the stored extraction for file_id=%s "
                "(%d chars); skipping the chain",
                getattr(doc, "file_id", "?"),
                len(text),
            )
            return text, stored.title
        # Stored, but empty. Re-running the chain would produce the same empty
        # result at full cost, so refuse here on the same terms the fresh path
        # refuses below — an agent that has read nothing is a broken feature,
        # not a slow one.
        raise ValidationError(
            "file.no_text",
            "No readable text was found in this file, so no agent was made from it.",
        )

    adapter = _resolve_adapter()
    storage_key = getattr(doc, "storage_key", "") or ""
    if adapter is None or not storage_key:
        raise Internal(
            "file.extraction_unavailable",
            "This file's storage is not reachable, so its text cannot be read.",
        )

    filename = getattr(doc, "filename", "") or "upload"
    mime = getattr(doc, "mime", "") or "application/octet-stream"

    async with materialize_to_local_path(
        adapter, storage_key, mime=mime, filename=filename
    ) as path:
        if path is None:
            raise Internal(
                "file.extraction_unavailable",
                "This file's bytes could not be read from storage.",
            )
        try:
            from pocketpaw.config import get_settings
            from pocketpaw_ee.cloud.extraction import build_chain

            chain = build_chain(get_settings())
            result = await chain.run(path, mime)
        except Exception as exc:
            logger.exception("book agent: extraction failed for file_id=%s", doc.file_id)
            raise Internal(
                "file.extraction_failed",
                "This file's text could not be extracted.",
            ) from exc

    text = (result.text or "").strip()
    if not text:
        # An image-only scan, or a format the chain can't read. Refused BEFORE
        # anything is created — a co-reader with nothing to read is not a
        # partial success. (The upload listener skips indexing in the same
        # case; this is the interactive equivalent of that skip.)
        raise ValidationError(
            "file.no_text",
            "No readable text was found in this file, so no agent was made from it.",
        )
    return text, result.title


async def ensure_book_agent(
    file_id: str,
    workspace_id: str,
    user_id: str,
) -> BookAgentResult:
    """Idempotently ensure ``file_id`` has a dedicated co-reader agent.

    Returns the bound agent's id plus whether THIS call created it and whether
    the book's text is in its knowledge scope. Raises ``NotFound`` for a file
    that does not exist in ``workspace_id`` (a cross-tenant press is a 404, not
    a 403 — the caller learns nothing about another tenant's rows).

    One agent per file, guaranteed twice over: by the persisted ``agent_id``
    bind and, if that is absent, by the deterministic slug. Never overwrites a
    live bind; a STALE bind (the agent was deleted) is not a permanent failure
    and re-provisions.
    """
    # Lazy imports, exactly as the concierge provisioner does it: the agents
    # service and the knowledge service pull in heavy trees, and keeping them
    # inside the call is what keeps this module's import edges inside the
    # existing import-linter contracts.
    from pocketpaw_ee.cloud._core.errors import ConflictError
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest

    if not file_id or not workspace_id:
        raise ValidationError("file.invalid", "A file id and workspace are required.")

    store = MongoFileStore()

    # (1) Load the file THROUGH the tenant filter. ``get_doc_scoped`` applies
    # workspace + not-deleted, so another workspace's file simply isn't found.
    doc = await store.get_doc_scoped(file_id, workspace_id)
    if doc is None:
        raise NotFound("file", file_id)

    # (2) FL-11b's privacy gate, enforced on this door too. A file the owner
    # has hidden from AI must not reach a KB scope by any route.
    if getattr(doc, "hide_from_ai", False):
        raise Forbidden(
            "file.hidden_from_ai",
            "This file is hidden from AI, so no agent can be made from it.",
        )

    # The FILE's workspace is authoritative for everything that follows — it
    # equals ``workspace_id`` by construction of the read above, and using the
    # row's own value keeps a future refactor of the read from widening tenancy.
    file_workspace = doc.workspace
    filename = doc.filename or "upload"

    # (3) Respect an existing LIVE bind — press twice, get the same agent, and
    # do no work at all (no re-extraction, no re-ingest). A bind is only ever
    # written after a successful ingest, so a live bind means indexed.
    existing_id = getattr(doc, "agent_id", "") or ""
    if existing_id:
        try:
            await agents_service.get(existing_id)
            return BookAgentResult(agent_id=existing_id, created=False, indexed=True)
        except NotFound:
            # Stale bind (agent deleted) — fall through and re-provision. Clear
            # it FIRST: if the re-provision then fails (unreadable file, ingest
            # error), the row would otherwise keep pointing at a deleted agent
            # and the library would offer "open" on a dead id. The invariant is
            # that a bind always names a live agent that has read the book.
            logger.info(
                "book agent: file %s bound to missing agent %s; re-provisioning",
                file_id,
                existing_id,
            )
            await store.set_book_agent(file_id, file_workspace, agent_id=None)

    # (4) Extract first — the name comes from the title, and a file we cannot
    # read must not leave an agent behind.
    text, title = await _extract_text(doc)
    book_name = book_agent_name(title, filename)

    # (5) Resolve-or-create the dedicated agent on the deterministic slug, so a
    # retry after a failed ingest adopts the agent already made rather than
    # minting a second one for the same book.
    slug = book_agent_slug(file_id)
    ctx = agents_service.legacy_ctx(user_id, file_workspace)

    created = False
    try:
        agent = await agents_service.get_by_slug(file_workspace, slug)
    except NotFound:
        agent = None

    if agent is None:
        body = CreateAgentRequest(
            name=book_name,
            slug=slug,
            # Workspace-visible like the concierge: the file's owner and the
            # admin who may have pressed the button both need to reach it.
            visibility="workspace",
            persona=book_agent_persona(book_name),
            soul_archetype=_BOOK_ARCHETYPE,
            # soul_enabled defaults True — a co-reader carries a soul.
        )
        try:
            agent = await agents_service.create(ctx, file_workspace, body)
            created = True
        except ConflictError:
            # Lost a create race on the deterministic slug — adopt the winner.
            agent = await agents_service.get_by_slug(file_workspace, slug)

    # (6) Give it the book. ``agent:{id}`` is the scope every turn of that
    # agent's chat already searches, so this one call is what "it has read the
    # book" means. Failure here is NOT fatal: the agent exists and the user can
    # see it, so we keep it, report ``indexed=False``, and leave the file
    # unbound so the next press retries the ingest against this same agent.
    indexed = await _ingest_book(agent.id, text, filename)
    if not indexed:
        return BookAgentResult(agent_id=agent.id, created=created, indexed=False)

    # (7) Bind the file to the agent — the idempotency key for the next press.
    # no-event: the visible artifact is the agent, and ``agents.service.create``
    # already emitted ``AgentCreated`` for it. This write is uploads-internal
    # bookkeeping on a row nothing subscribes to; an event here would announce
    # the same thing twice, and on the adopt path announce nothing new at all.
    bound = await store.set_book_agent(file_id, file_workspace, agent_id=agent.id)
    if bound is None:
        # The row vanished under us (deleted mid-provision). The agent read the
        # book and still works; only the shortcut back to it is lost.
        logger.warning(
            "book agent: agent %s ingested %s but the file row was gone at bind time",
            agent.id,
            file_id,
        )
    return BookAgentResult(agent_id=agent.id, created=created, indexed=True)


async def _ingest_book(agent_id: str, text: str, filename: str) -> bool:
    """Ingest the book into ``agent:{id}``; ``True`` on success.

    Swallows the failure ON PURPOSE (see the module's failure policy): the
    caller has an agent it must not throw away, and the boolean is how that
    partial state travels back to the user instead of a 500.
    """
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    try:
        await KnowledgeService.ingest_text_to_scope(
            scope=f"agent:{agent_id}",
            text=text,
            source=filename,
        )
        return True
    except Exception:
        logger.exception(
            "book agent: KB ingest failed for agent %s (source=%s); the agent "
            "exists but has not read the book — the file stays unbound so the "
            "next press retries",
            agent_id,
            filename,
        )
        return False


__all__ = [
    "BookAgentResult",
    "book_agent_name",
    "book_agent_persona",
    "book_agent_slug",
    "ensure_book_agent",
]
