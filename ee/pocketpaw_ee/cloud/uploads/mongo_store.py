"""Mongo-backed metadata store, workspace-scoped.

2026-08-29 (T0 "Persist the extracted text"): added ``set_extracted_text`` —
the workspace-scoped setter ``uploads.extracted_text`` uses to point a row at
the derived blob holding its ``ExtractionResult``, shaped exactly like
``set_kb_article`` (read through the workspace filter, write the pair, save).
The key and the ``content_version`` it was extracted from are always written
TOGETHER: a key without a version is a blob whose freshness nobody can judge.
``extracted_text`` never touches Beanie itself; this store stays the only owner
of ``FileUpload`` writes in the package. Deliberately NOT threaded into
``_to_record`` or the ``iter_by_*`` dict rows — unlike ``tags``/``summary``,
nothing in a listing renders it, and ``FileRecord`` is a ``src/`` type whose
shape ripples well past this package.

2026-08-28 (FC-1 "File comprehension"): ``summary`` is threaded exactly the way
``tags`` is — ``set_library_metadata`` grew a ``summary`` keyword, and both
``iter_by_workspace`` / ``iter_by_pocket`` dict rows now carry it so the
/files listing can show what a file IS without a second query. Same
only-touch-what-was-passed rule as the other library fields, and the same
always-applied workspace filter, so a caller still cannot write across tenants.

2026-08-29 (BA-1 "Make an agent of this book"): added ``set_book_agent`` —
the workspace-scoped setter ``uploads.book_agent`` uses to bind a file to the
dedicated co-reader agent made from it, shaped exactly like ``set_kb_article``
(read the row through the workspace filter, then write). ``book_agent`` never
touches Beanie itself; this store stays the only owner of ``FileUpload``
writes in the package. The ``iter_by_workspace`` / ``iter_by_pocket`` dict rows
also carry ``agent_id`` now, so the /files listing can render "open the agent"
instead of "make one" without a second query — the same way ``tags`` is
threaded.

2026-08-04 (Living-wiki API): ``iter_by_workspace`` rows now also carry
``kb_article_id`` and ``kb_scope`` (the FL-11b ingest-tracking columns) so
GET /knowledge/uploads can derive ``has_article`` without a second query,
plus ``pocket_id`` so workspace-level listings can exclude pocket-private
rows. Added ``reassign_kb_article`` — after a reingest recompiles an
article under a new slug, the tracking rows pointing at the old id are
moved to the new one so a later hide-from-AI purge hits the live copy.
Additive — existing consumers of the dict shape are unchanged.
2026-07-03 (FL-11b "hide-from-AI purge"): added ``set_kb_article`` — a
workspace-scoped setter the FileReady listener uses to record the kb-go
``article_id`` + ``scope`` on a row after a successful ingest, and the PATCH
route uses (with ``None`` args) to clear that tracking after a purge. The
workspace filter is always applied on the read, so cross-tenant writes are
impossible through this API.
2026-07-03 (FL-1 "Library metadata"): the ``iter_by_workspace`` /
``iter_by_pocket`` dict rows now carry ``collections`` and ``hide_from_ai``
alongside the pre-existing ``tags`` so library metadata round-trips into the
/files listing. Added ``set_library_metadata`` — a workspace-scoped setter the
PATCH /uploads/{id} route uses to update ``tags`` / ``collections`` /
``hide_from_ai`` on one row (only the provided fields are touched). The
workspace filter is always applied on the read, so cross-tenant writes are
impossible through this API.
2026-04-19 (Cluster E sub-PR 4): added ``list_by_workspace`` so the
unified files endpoint can pull chat-sourced uploads alongside local
filesystem entries. Soft-deleted rows are skipped. Results are capped
to keep the unified list cheap.
2026-05-03 (Stage 3.E "Files as Knowledge"): ``save_scoped`` now
accepts ``pocket_id`` so pocket uploads carry the metadata column
through to ``FileUpload``. Reads grew a ``pocket_id`` filter on
``list_by_workspace`` and a symmetric ``iter_by_pocket`` for the
unified files panel. Storage layout is unchanged; partitioning is
metadata-only (Captain Option A).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pocketpaw.uploads.file_store import FileRecord
from pocketpaw_ee.cloud.uploads.models import FileUpload


class _Sentinel:
    """Distinct type for the ``pocket_id IS None`` filter sentinel.

    Plain ``None`` already means "don't filter" on the legacy
    ``list_by_workspace`` API; we need a separate value so callers can
    explicitly ask for workspace-only rows (``pocket_id is None``) without
    overloading ``None`` to mean both.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — debugging only
        return "LIST_WORKSPACE_ONLY"


LIST_WORKSPACE_ONLY = _Sentinel()
"""Sentinel: pass as ``pocket_id`` to filter rows where ``pocket_id IS None``."""


class MongoFileStore:
    """Workspace-scoped metadata store for EE uploads."""

    async def save_scoped(
        self,
        record: FileRecord,
        workspace: str,
        *,
        folder_path: str = "/",
        pocket_id: str | None = None,
    ) -> None:
        doc = FileUpload(
            file_id=record.id,
            storage_key=record.storage_key,
            filename=record.filename,
            mime=record.mime,
            size=record.size,
            workspace=workspace,
            owner=record.owner_id,
            chat_id=record.chat_id,
            folder_path=folder_path or "/",
            pocket_id=pocket_id,
        )
        await doc.insert()

    async def get_doc_scoped(self, file_id: str, workspace: str) -> FileUpload | None:
        return await FileUpload.find_one(
            FileUpload.file_id == file_id,
            FileUpload.workspace == workspace,
            FileUpload.deleted_at == None,  # noqa: E711
        )

    async def set_library_metadata(
        self,
        file_id: str,
        workspace: str,
        *,
        tags: list[str] | None = None,
        collections: list[str] | None = None,
        hide_from_ai: bool | None = None,
        summary: str | None = None,
    ) -> FileUpload | None:
        """Set library metadata on one live row, workspace-scoped (FL-1, FC-1).

        Only the fields passed as non-``None`` are updated — omitted fields
        keep their current value. Returns the updated doc, or ``None`` if no
        live row matches ``(file_id, workspace)``. The workspace filter is
        always applied, so a caller cannot mutate another tenant's rows.

        ``summary`` (FC-1) follows the same rule, which means this setter
        cannot CLEAR a summary — ``None`` is "leave it alone", not "erase it".
        That is deliberate and matches the other fields; the only writer that
        would want to erase one is a human, and the PATCH route handles an
        explicit empty string by writing ``""``.
        """
        doc = await self.get_doc_scoped(file_id, workspace)
        if doc is None:
            return None
        if tags is not None:
            doc.tags = [str(t) for t in tags]
        if collections is not None:
            doc.collections = [str(c) for c in collections]
        if hide_from_ai is not None:
            doc.hide_from_ai = bool(hide_from_ai)
        if summary is not None:
            doc.summary = str(summary)
        await doc.save()
        return doc

    async def set_kb_article(
        self,
        file_id: str,
        workspace: str,
        *,
        article_id: str | None,
        scope: str | None,
    ) -> FileUpload | None:
        """Record (or clear) the tracked kb-go article on one row (FL-11b).

        Workspace-scoped: the workspace filter is always applied, so a caller
        cannot mutate another tenant's rows. Pass a non-empty ``article_id`` +
        ``scope`` after a successful ingest to enable a later purge; pass
        ``None`` for both to clear the tracking after a purge (so a re-index
        re-populates it). Both fields are always written to the given values.
        Returns the updated doc, or ``None`` if no live row matches
        ``(file_id, workspace)``.
        """
        doc = await self.get_doc_scoped(file_id, workspace)
        if doc is None:
            return None
        doc.kb_article_id = article_id
        doc.kb_scope = scope
        await doc.save()
        return doc

    async def set_book_agent(
        self,
        file_id: str,
        workspace: str,
        *,
        agent_id: str | None,
    ) -> FileUpload | None:
        """Bind (or clear) this file's dedicated book agent (BA-1).

        Workspace-scoped like every other write here — the filter is applied on
        the read, so a caller cannot bind another tenant's row. Pass an agent id
        after the book's text has landed in that agent's KB scope; pass ``None``
        to clear a bind (e.g. the agent was deleted). Returns the updated doc,
        or ``None`` if no live row matches ``(file_id, workspace)``.
        """
        doc = await self.get_doc_scoped(file_id, workspace)
        if doc is None:
            return None
        doc.agent_id = agent_id
        await doc.save()
        return doc

    async def set_extracted_text(
        self,
        file_id: str,
        workspace: str,
        *,
        key: str | None,
        content_version: int | None,
    ) -> FileUpload | None:
        """Point a row at its persisted extraction blob (T0).

        Workspace-scoped like every other write here — the filter is applied on
        the read, so a caller cannot re-point another tenant's row. Pass the
        storage key plus the ``content_version`` the text was extracted FROM
        after a successful blob write; pass ``None`` for both to clear the
        pointer (the reader then falls back to a live extraction). Both fields
        are always written to the given values, the way ``set_kb_article``
        writes its pair — they only mean anything together, so a partial update
        would leave a key whose freshness cannot be judged.

        Returns the updated doc, or ``None`` if no live row matches
        ``(file_id, workspace)``.
        """
        doc = await self.get_doc_scoped(file_id, workspace)
        if doc is None:
            return None
        doc.extracted_text_key = key
        doc.extracted_text_version = content_version
        await doc.save()
        return doc

    async def reassign_kb_article(
        self,
        workspace: str,
        *,
        old_article_id: str,
        new_article_id: str,
        scope: str,
    ) -> int:
        """Move FL-11b tracking from one article id to another (living-wiki).

        A reingest can recompile an article under a NEW slug; any upload row
        still tracking the old id would purge a dead article on a later
        hide-from-AI toggle while the live copy survives. Workspace-scoped
        like every other write here. Returns the number of rows updated
        (usually 0 or 1). No-op when the ids are equal.
        """
        if old_article_id == new_article_id:
            return 0
        updated = 0
        docs = await FileUpload.find(
            FileUpload.workspace == workspace,
            FileUpload.kb_article_id == old_article_id,
        ).to_list()
        for doc in docs:
            doc.kb_article_id = new_article_id
            doc.kb_scope = scope
            await doc.save()
            updated += 1
        return updated

    async def rewrite_folder_prefix(
        self,
        workspace: str,
        old_prefix: str,
        new_prefix: str,
    ) -> int:
        """Rewrite ``folder_path`` on every live file under ``old_prefix``.

        Handles the row AT ``old_prefix`` (``folder_path == old_prefix``)
        plus strict descendants (``folder_path`` starts with ``old_prefix + "/"``).
        Returns count updated. Retry-safe: files already under ``new_prefix``
        are left alone.
        """
        if old_prefix == new_prefix:
            return 0
        count = 0
        cursor = FileUpload.find(
            FileUpload.workspace == workspace,
            FileUpload.deleted_at == None,  # noqa: E711
        )
        async for d in cursor:
            fp = d.folder_path or "/"
            if fp == old_prefix:
                d.folder_path = new_prefix
                await d.save()
                count += 1
            elif old_prefix != "/" and fp.startswith(old_prefix + "/"):
                d.folder_path = new_prefix + fp[len(old_prefix) :]
                await d.save()
                count += 1
        return count

    async def soft_delete_under_prefix(self, workspace: str, prefix: str) -> int:
        """Soft-delete every live file under ``prefix`` (at or below)."""
        count = 0
        now = datetime.now(UTC)
        cursor = FileUpload.find(
            FileUpload.workspace == workspace,
            FileUpload.deleted_at == None,  # noqa: E711
        )
        async for d in cursor:
            fp = d.folder_path or "/"
            if fp == prefix or (prefix != "/" and fp.startswith(prefix + "/")):
                d.deleted_at = now
                await d.save()
                count += 1
        return count

    async def count_under_prefix(self, workspace: str, prefix: str) -> int:
        count = 0
        cursor = FileUpload.find(
            FileUpload.workspace == workspace,
            FileUpload.deleted_at == None,  # noqa: E711
        )
        async for d in cursor:
            fp = d.folder_path or "/"
            if fp == prefix or (prefix != "/" and fp.startswith(prefix + "/")):
                count += 1
        return count

    async def get_scoped(self, file_id: str, workspace: str) -> FileRecord | None:
        doc = await FileUpload.find_one(
            FileUpload.file_id == file_id,
            FileUpload.workspace == workspace,
            FileUpload.deleted_at == None,  # noqa: E711 beanie needs literal None
        )
        return self._to_record(doc)

    async def get_unscoped(self, file_id: str) -> FileRecord | None:
        """Find a live record by file_id without workspace filter.

        Intended for call sites that lack tenant context (e.g. the OSS chat
        bridge in single-user self-hosted deployments). Multi-tenant cloud
        chat flows should use ``get_scoped`` with an authenticated workspace
        and never call this.
        """
        doc = await FileUpload.find_one(
            FileUpload.file_id == file_id,
            FileUpload.deleted_at == None,  # noqa: E711
        )
        return self._to_record(doc)

    @staticmethod
    def _to_record(doc: FileUpload | None) -> FileRecord | None:
        if doc is None:
            return None
        return FileRecord(
            id=doc.file_id,
            storage_key=doc.storage_key,
            filename=doc.filename,
            mime=doc.mime,
            size=doc.size,
            owner_id=doc.owner,
            chat_id=doc.chat_id,
            created=doc.createdAt or datetime.now(UTC),
            # Library metadata, read defensively: legacy rows predate every
            # one of these fields. This is the path the flat ``GET /files``
            # listing is built from, so a field missing HERE is a field the
            # Files panel can never render no matter what the DB holds.
            tags=list(getattr(doc, "tags", None) or []),
            collections=list(getattr(doc, "collections", None) or []),
            summary=getattr(doc, "summary", None),
            agent_id=getattr(doc, "agent_id", None),
        )

    async def iter_by_workspace(
        self,
        workspace: str,
        *,
        include_deleted: bool = False,
        limit: int = 500,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield upload docs for a workspace as plain dicts.

        Used by the unified files module (ee/cloud/files) to surface the
        chat-source slice. Keeps the shape minimal — callers convert to their
        own row types.
        """
        query: list[Any] = [FileUpload.workspace == workspace]
        if not include_deleted:
            query.append(FileUpload.deleted_at == None)  # noqa: E711
        cursor = FileUpload.find(*query).sort([("createdAt", -1)]).limit(limit)
        async for doc in cursor:
            created = doc.createdAt
            updated = getattr(doc, "updatedAt", None) or created
            yield {
                "file_id": doc.file_id,
                "filename": doc.filename,
                "mime": doc.mime,
                "size": doc.size,
                # Legacy keys (workspace/owner) retained for back-compat.
                "workspace": doc.workspace,
                "owner": doc.owner,
                # Canonical keys used by ee.cloud.files providers.
                "workspace_id": doc.workspace,
                "owner_id": doc.owner,
                "chat_id": doc.chat_id,
                "folder_path": getattr(doc, "folder_path", None) or "/",
                "created_at": created,
                "updated_at": updated,
                "tags": list(getattr(doc, "tags", []) or []),
                "collections": list(getattr(doc, "collections", []) or []),
                "hide_from_ai": bool(getattr(doc, "hide_from_ai", False)),
                # FC-1: read defensively — legacy rows predate the field.
                "summary": getattr(doc, "summary", None),
                # Living-wiki API: FL-11b ingest tracking, so /knowledge/uploads
                # can derive has_article without a second query; pocket_id so
                # workspace-level listings can exclude pocket-private rows.
                "kb_article_id": getattr(doc, "kb_article_id", None),
                "kb_scope": getattr(doc, "kb_scope", None),
                "pocket_id": getattr(doc, "pocket_id", None),
                # BA-1: the dedicated co-reader agent made from this file, so
                # the listing can offer "open" vs "make" without a second read.
                "agent_id": getattr(doc, "agent_id", None),
            }

    async def list_by_workspace(
        self,
        workspace: str,
        *,
        limit: int = 200,
        chat_id: str | None = None,
        pocket_id: str | None | _Sentinel = None,
    ) -> list[FileRecord]:
        """Return live (non-deleted) file records in a workspace.

        Newest first. When ``chat_id`` is supplied, narrow further to the
        uploads that originated in that chat. The workspace filter always
        applies — cross-workspace bleed is not allowed through this API.

        ``pocket_id`` is tri-state:
        - ``None`` (default): no pocket filter applied (legacy behaviour;
          returns workspace-scoped + pocket-scoped rows alike).
        - A string id: filter to rows scoped to that pocket.
        - ``LIST_WORKSPACE_ONLY`` sentinel: filter to ``pocket_id IS None``
          rows (workspace-scoped uploads only — what the workspace Files
          panel surfaces).
        """
        capped = max(1, min(limit, 500))
        query: dict = {
            "workspace": workspace,
            "deleted_at": None,
        }
        if chat_id:
            query["chat_id"] = chat_id
        if pocket_id is LIST_WORKSPACE_ONLY:
            query["pocket_id"] = None
        elif isinstance(pocket_id, str):
            query["pocket_id"] = pocket_id
        docs = await FileUpload.find(query).sort([("createdAt", -1)]).limit(capped).to_list()
        return [r for r in (self._to_record(d) for d in docs) if r is not None]

    async def count_by_workspace(
        self,
        workspace: str,
        *,
        pocket_id: str | None | _Sentinel = None,
    ) -> int:
        """Count live (non-deleted) file records in a workspace.

        Mirrors ``list_by_workspace``'s tri-state ``pocket_id`` so the
        count always describes exactly the rows a matching list call would
        return:
        - ``None`` (default): no pocket filter applied.
        - A string id: rows scoped to that pocket.
        - ``LIST_WORKSPACE_ONLY`` sentinel: rows with ``pocket_id IS None``
          (workspace-scoped uploads only — what the workspace Files panel
          surfaces).
        """
        query: dict = {
            "workspace": workspace,
            "deleted_at": None,
        }
        if pocket_id is LIST_WORKSPACE_ONLY:
            query["pocket_id"] = None
        elif isinstance(pocket_id, str):
            query["pocket_id"] = pocket_id
        return await FileUpload.find(query).count()

    async def iter_by_pocket(
        self,
        workspace: str,
        pocket_id: str,
        *,
        include_deleted: bool = False,
        limit: int = 500,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield upload docs for a single pocket as plain dicts.

        Symmetric with :meth:`iter_by_workspace`. Used by the unified
        files endpoint when the FE asks for a pocket-scoped listing.
        Always includes a workspace filter so cross-workspace bleed is
        impossible via this API.
        """
        query: list[Any] = [
            FileUpload.workspace == workspace,
            FileUpload.pocket_id == pocket_id,
        ]
        if not include_deleted:
            query.append(FileUpload.deleted_at == None)  # noqa: E711
        cursor = FileUpload.find(*query).sort([("createdAt", -1)]).limit(limit)
        async for doc in cursor:
            created = doc.createdAt
            updated = getattr(doc, "updatedAt", None) or created
            yield {
                "file_id": doc.file_id,
                "filename": doc.filename,
                "mime": doc.mime,
                "size": doc.size,
                "workspace": doc.workspace,
                "owner": doc.owner,
                "workspace_id": doc.workspace,
                "owner_id": doc.owner,
                "chat_id": doc.chat_id,
                "pocket_id": doc.pocket_id,
                "folder_path": getattr(doc, "folder_path", None) or "/",
                "created_at": created,
                "updated_at": updated,
                "tags": list(getattr(doc, "tags", []) or []),
                "collections": list(getattr(doc, "collections", []) or []),
                "hide_from_ai": bool(getattr(doc, "hide_from_ai", False)),
                # FC-1: read defensively — legacy rows predate the field.
                "summary": getattr(doc, "summary", None),
                # BA-1: same bind the workspace listing carries — a pocket's
                # Files panel offers the book agent on the same terms.
                "agent_id": getattr(doc, "agent_id", None),
            }

    async def count_by_pocket(self, workspace: str, pocket_id: str) -> int:
        """Count live (non-deleted) files in a pocket. Cheap, one query."""
        return await FileUpload.find(
            FileUpload.workspace == workspace,
            FileUpload.pocket_id == pocket_id,
            FileUpload.deleted_at == None,  # noqa: E711
        ).count()

    async def soft_delete_scoped(self, file_id: str, workspace: str) -> None:
        doc = await FileUpload.find_one(
            FileUpload.file_id == file_id,
            FileUpload.workspace == workspace,
        )
        if doc is None:
            return
        doc.deleted_at = datetime.now(UTC)
        await doc.save()
