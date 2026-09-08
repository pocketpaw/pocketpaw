"""EE FileUpload document — Mongo metadata for blobs stored via StorageAdapter.

2026-09-05 — files vault (feat/files-links). Added ``link_names`` (list[str],
default empty): the normalized ``[[wikilink]]`` targets the FileReady listener
parses out of a markdown / plain-text note. Resolved to files at read time by
``normalize_link_name(filename)``, so link order and renames never need a
rewrite here. Indexed ``(workspace, link_names)`` for the backlinks read. Beanie
field-add with a default, legacy rows read back ``[]``.

2026-08-29 — T0 "Persist the extracted text". Added ``extracted_text_key``
(``str | None``) and ``extracted_text_version`` (``int | None``), both default
``None``. Together they point at ONE derived blob holding the serialized
``ExtractionResult`` produced at upload time, so no later consumer has to run
the extraction chain over the same bytes again (``uploads/extracted_text.py``
owns the blob; see that module for the read/write contract).

**Why a BLOB and not a Mongo field.** A 500-page book extracts to ~940K chars
(measured). Three things break if that text lives on this document:

1. ``mongo_store.iter_by_workspace`` / ``iter_by_pocket`` / ``list_by_workspace``
   hydrate FULL Beanie documents with ``limit`` up to 500 and no projection.
   500 rows x ~1 MB is a memory bomb on the plain ``/files`` listing, and the
   only fix would be a projection on every read path — including every read
   path anyone adds later, which is a rule that silently stops being followed.
2. BSON caps a document at 16 MB. An extraction that crosses it does not
   degrade, it makes ``doc.save()`` RAISE — which would take the ``summary``,
   ``tags`` and ``kb_article_id`` writes on the same row down with it.
3. Capping the field to fit is not a way out: the whole point of persisting the
   text is that the BOOK AGENT can reuse it, and the book agent needs the whole
   book. A cap makes the feature miss exactly the file it exists for.

A blob has none of those limits, and this package already owns a
``StorageAdapter`` keyed by string. The key is DETERMINISTIC on ``file_id``
(not a random ``new_storage_key``) so a re-ingest overwrites in place instead
of leaking a new object per pass, and so a blob written while the column write
failed is simply overwritten next time rather than orphaned forever.

``extracted_text_version`` is the staleness guard, and it is load-bearing:
``cloud/file_versions/service.py`` rewrites a file's bytes and bumps
``content_version`` WITHOUT emitting ``FileReady``, so an inline edit never
re-runs extraction. It records the ``content_version`` the text was extracted
from; the reader treats "version does not match the row's current
``content_version``" as identical to "no stored text" and falls back to a live
extraction. Without it, persisting text would REGRESS the book agent, which
re-extracts fresh today.

Neither field is indexed: both are read only after a row has already been
resolved by ``file_id``, exactly like ``summary`` and ``kb_article_id``.
Legacy rows read back ``None`` via the defaults (Beanie field-add, no
migration) and every consumer falls back to extraction on ``None``.

Known follow-up, deliberately NOT built here: flipping ``hide_from_ai`` on does
not yet PURGE an already-persisted text blob the way it purges the kb article.
The reader refuses to serve text for a hidden file, so nothing reads it — but
the bytes stay at rest until the file is deleted. The purge belongs beside the
existing kb purge in ``uploads/router.py``'s PATCH handler.

2026-08-28 — FC-1 "File comprehension". Added ``summary`` (``str | None``,
default ``None``): one or two sentences saying what the file IS, written by the
comprehension pass on ingest (``uploads/comprehension.py``) and editable by a
human through ``PATCH /uploads/{id}``. Legacy rows read back ``None`` via the
default — the same backward-compatible Beanie field-add FL-1 relied on, so
there is no migration. Deliberately NOT indexed: nothing filters or sorts on a
summary, it is only ever read after a row has been resolved by ``file_id``, and
an index on free text would cost writes for a query nobody makes. The
comprehension pass also writes ``collections`` (declared by FL-1 but, until
now, never written by anything).

2026-08-29 — BA-1 "Make an agent of this book". Added ``agent_id``
(``str | None``, default ``None``) so a file REMEMBERS the dedicated
co-reader agent provisioned from it. Without the column, pressing "Make an
agent of this book" twice would mint two agents for one book. The bind is
written by ``uploads.book_agent`` only AFTER the book text lands in the
agent's KB scope — an agent that exists but hasn't read the book yet stays
unbound on purpose, so the next press retries the ingest instead of
returning a co-reader that knows nothing. Legacy rows read back ``None``
via the default (Beanie field-add, no migration). Not indexed: it's read
only after a row has already been resolved by ``file_id``.

2026-07-03 — FL-11b "hide-from-AI purge". Added ``kb_article_id`` and
``kb_scope`` (both ``str | None``, default ``None``) so a row remembers the
kb-go article it was ingested into. The FileReady listener records them after a
successful ingest; the PATCH route reads them to purge that article when a file
is later hidden from AI (``hide_from_ai`` flips false→true). Legacy rows read
back ``None`` via the defaults — no migration needed (Beanie field-add). Not
indexed: they're read only after resolving a row by ``file_id``.

2026-07-03 — FL-1 "Library metadata". Added ``tags`` (list[str]),
``collections`` (list[str]) and ``hide_from_ai`` (bool) so a file can carry
library organization + an AI-visibility flag that persist and round-trip
through the /files listing. Legacy rows without the fields read back as empty
lists / False via the Pydantic defaults (no migration script needed — Beanie
field-add with a default is backward compatible). ``tags`` and ``collections``
each get a Mongo index so library filtering stays cheap. ``tags`` was already
read defensively via ``getattr`` in ``mongo_store``; this formalizes it as a
declared field alongside the two new ones.

2026-06-26 — ART-1. Added ``content_version`` (default 0) so the
``file_versions`` service can run optimistic-concurrency edits and archive
each prior blob as a ``FileVersionDoc``. Legacy rows read as 0; ``write_file``
stamps new rows at 1. Beanie field-add is backward compatible (no migration).

2026-05-03 — Stage 3.E "Files as Knowledge". Added ``pocket_id`` so the
unified Files panel and the FileReady listener can route a single upload
into a pocket-scoped KB instead of the workspace pool. ``None`` is the
right semantic for "workspace upload" (no migration script needed; Beanie
field-add is backward compatible).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from beanie import Document, Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class FileUpload(TimestampedDocument):
    """Metadata for one uploaded file. Blob bytes live in the StorageAdapter.

    Distinct from ``ee.cloud.models.file.FileObj`` (pre-signed URL storage):
    ``FileUpload`` is the adapter-backed path for chat attachments, with
    workspace scoping and soft-delete.
    """

    file_id: Indexed(str, unique=True)  # type: ignore[valid-type]
    storage_key: str
    filename: str
    mime: str
    size: int
    workspace: Indexed(str)  # type: ignore[valid-type]
    owner: str
    chat_id: Indexed(str) | None = None  # type: ignore[valid-type]
    # Absolute folder path for the "My Files" mount. Root is ``"/"``.
    # Missing/None on legacy rows → treat as root.
    folder_path: str | None = "/"
    # Pocket scoping (Stage 3.E). ``None`` = workspace-scoped (default,
    # the pre-Stage-3.E semantics). When set, the file shows up only in
    # the pocket's Files panel and gets indexed into ``pocket:{id}`` KB.
    # Storage layout is unchanged — partitioning is metadata-only.
    pocket_id: str | None = None
    deleted_at: datetime | None = None
    # Optimistic-concurrency counter for the file_versions edit pipeline
    # (ART-1). Each successful inline edit bumps this and archives the prior
    # blob as a ``FileVersionDoc``. 0 on legacy rows; ``write_file`` stamps 1.
    content_version: int = 0
    # Library metadata (FL-1). Free-form ``tags`` and named ``collections``
    # organize the file in the library UI; ``hide_from_ai`` opts a file out of
    # AI/KB visibility. Legacy rows without these read back as empty lists /
    # False via the defaults — no migration script needed.
    tags: list[str] = Field(default_factory=list)
    collections: list[str] = Field(default_factory=list)
    hide_from_ai: bool = False
    # What this file IS, in a sentence or two (FC-1). Written by the
    # comprehension pass on ingest and by a human through PATCH. ``None`` on
    # legacy rows and on files that were never comprehended (hidden from AI,
    # over the daily cap, or the model call failed) — the library UI treats
    # ``None`` and "" alike: no summary to show. Not indexed; see the module
    # docstring for why.
    summary: str | None = None
    # KB tracking (FL-11b). After a successful ingest the FileReady listener
    # records the kb-go article id and the scope it landed in, so the file can
    # be retroactively purged from the KB if it's later hidden from AI. ``None``
    # means "not (currently) indexed" — a hide toggle then skips the purge, and
    # a later re-index re-populates these. Legacy rows read ``None``.
    kb_article_id: str | None = None
    kb_scope: str | None = None
    # Persisted extraction (T0). ``extracted_text_key`` is the storage key of
    # the derived blob holding the serialized ``ExtractionResult`` from ingest;
    # ``extracted_text_version`` is the ``content_version`` those bytes were
    # extracted FROM. A reader that sees a mismatch must treat the blob as
    # absent and re-extract — see the module docstring for why the version is
    # not optional. ``None`` on legacy rows, on files that were never
    # extracted, and on files whose persist step failed (the ingest still
    # succeeded; the only cost is that the next consumer re-extracts).
    extracted_text_key: str | None = None
    extracted_text_version: int | None = None
    # Book-agent bind (BA-1). The id of the dedicated co-reader agent made
    # from this file, or ``None`` when none has been made. This is the
    # idempotency key for "Make an agent of this book": a live bind short-
    # circuits the whole provision path. Written only after a successful
    # ingest — see the module docstring for why a half-provisioned agent
    # deliberately leaves this ``None``.
    agent_id: str | None = None
    # Normalized wikilink targets parsed from note text (files vault). Empty for
    # anything that is not a text note.
    link_names: list[str] = Field(default_factory=list)

    class Settings:
        name = "file_uploads"
        indexes = [
            [("workspace", 1), ("chat_id", 1), ("createdAt", -1)],
            [("workspace", 1), ("owner", 1), ("createdAt", -1)],
            [("workspace", 1), ("folder_path", 1), ("deleted_at", 1)],
            # Stage 3.E: pocket-scoped queries hit this index. Newest
            # first because the Files panel orders by ``created`` desc.
            [("workspace", 1), ("pocket_id", 1), ("createdAt", -1)],
            # FL-1: library filtering by tag / collection within a workspace.
            [("workspace", 1), ("tags", 1)],
            [("workspace", 1), ("collections", 1)],
            # Files vault: backlinks ("which notes link to this name").
            [("workspace", 1), ("link_names", 1)],
        ]


class FileFolder(Document):
    """Folder node for the "My Files" mount (uploads provider only).

    Folders are workspace-scoped, owner-stamped, soft-deleted. They exist
    only for the uploads provider — other providers (kb, chat, drive,
    local) stay flat in this release.
    """

    folder_id: str = Field(default_factory=lambda: uuid4().hex)
    workspace: str
    owner: str
    path: str  # absolute normalized, e.g. "/reports/2026"
    name: str  # final segment of ``path``
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None

    class Settings:
        name = "file_folders"
        indexes = [
            [("workspace", 1), ("path", 1)],
            [("workspace", 1), ("owner", 1), ("deleted_at", 1)],
        ]
