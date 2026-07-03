# library_verbs.py — agent Library verbs (FL-3): tag / move / annotate / search.
# Created: 2026-07-03 (FL-3) — the single-file verbs that make /files agentic.
#   Four BaseTool subclasses the agent uses to organize + read the workspace
#   Library:
#     - tag_file(file_id, add?, remove?)  → metadata op, journal event
#     - move_file(file_id, folder_path)   → metadata op, journal event
#     - annotate_file(file_id, note)      → CONTENT op → new FL-2 version (revertable)
#     - search_library(query, limit)      → BM25 search over the workspace KB scope
#   Every verb is workspace-scoped: the workspace/user come from the per-stream
#   agent identity ContextVars (``current_workspace_id`` / ``current_user_id`` in
#   ee.cloud.chat.agent_service), so a verb can only ever touch the CURRENT
#   workspace's files — cross-workspace access is impossible (the store lookups
#   and the FL-2 service both pin workspace_id). Metadata writes go through FL-1's
#   ``MongoFileStore``; the content mutation goes through FL-2's
#   ``file_versions.service.annotate_upload`` so annotations archive as
#   ``FileVersionDoc`` rows and stay revertable. Mutating verbs are trust_level
#   "medium" (not auto-approved), matching the fabric/instinct write-tool
#   convention; ``search_library`` is a read → "high".

from __future__ import annotations

import logging
from typing import Any

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workspace / user resolution
#
# The agent runtime binds the active stream's tenancy onto ContextVars in
# ``ee.cloud.chat.agent_service`` (set by ``attach_agent_identity`` on every
# cloud chat dispatch). These verbs read from there so they never need a
# FastAPI request scope — the same seam the in-process pocket-write MCP tools
# use. Imported lazily so a community (non-EE) install still loads the module
# without the cloud package present.
# ---------------------------------------------------------------------------


def _current_workspace() -> str | None:
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_workspace_id

        return current_workspace_id()
    except ImportError:
        return None


def _current_user() -> str | None:
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id

        return current_user_id()
    except ImportError:
        return None


def _mongo_store():
    """The FL-1 workspace-scoped upload metadata store (lazy import)."""
    try:
        from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

        return MongoFileStore()
    except ImportError:
        return None


def _request_ctx(workspace_id: str, user_id: str | None):
    """Build a minimal RequestContext for the FL-2 file_versions service."""
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id=user_id or "agent",
        workspace_id=workspace_id,
        request_id="",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


async def _emit_library_event(event_type: str, data: dict[str, Any]) -> None:
    """Emit a journal/bus event for a Library metadata mutation.

    Best-effort: a bus that isn't initialized (a unit test that skips
    ``init_realtime``) must not break the verb — the DB write already
    succeeded. Mirrors the ``emit`` facade's own "never raise back" contract.
    """
    try:
        from pocketpaw_ee.cloud._core.realtime.emit import emit
        from pocketpaw_ee.cloud._core.realtime.events import Event

        await emit(Event(type=event_type, data=data))
    except Exception:
        logger.debug("library event emit skipped (type=%s)", event_type, exc_info=True)


# ---------------------------------------------------------------------------
# tag_file
# ---------------------------------------------------------------------------


class TagFileTool(BaseTool):
    """Add or remove tags on a Library file."""

    @property
    def name(self) -> str:
        return "tag_file"

    @property
    def description(self) -> str:
        return (
            "Add or remove tags on a file in the workspace Library. Tags organize "
            "and filter files (e.g. 'invoice', 'q3', 'archived'). Pass 'add' to "
            "attach tags and/or 'remove' to detach them; both merge against the "
            "file's current tags. Operates only on files in the current workspace."
        )

    @property
    def trust_level(self) -> str:
        return "medium"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "ID of the Library file to tag.",
                },
                "add": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to add.",
                },
                "remove": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to remove.",
                },
            },
            "required": ["file_id"],
        }

    async def execute(
        self,
        file_id: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> str:
        workspace = _current_workspace()
        if not workspace:
            return self._error("Library verbs require a workspace context (cloud chat session).")

        store = _mongo_store()
        if store is None:
            return self._error("Library is not available (enterprise feature).")

        if not add and not remove:
            return self._error("Provide at least one tag to add or remove.")

        doc = await store.get_doc_scoped(file_id, workspace)
        if doc is None:
            return self._error(f"File {file_id!r} not found in this workspace.")

        current = list(doc.tags or [])
        add_set = {str(t).strip() for t in (add or []) if str(t).strip()}
        remove_set = {str(t).strip() for t in (remove or [])}
        # Preserve order: keep existing (minus removed), then append new.
        merged = [t for t in current if t not in remove_set]
        for t in add_set:
            if t not in merged:
                merged.append(t)

        updated = await store.set_library_metadata(file_id, workspace, tags=merged)
        if updated is None:
            return self._error(f"File {file_id!r} not found in this workspace.")

        await _emit_library_event(
            "file.tagged",
            {
                "file_id": file_id,
                "workspace_id": workspace,
                "tags": merged,
                "added": sorted(add_set),
                "removed": sorted(remove_set),
                "actor": _current_user() or "agent",
            },
        )
        return self._success(
            f"Tagged {updated.filename} ({file_id}). Tags now: {', '.join(merged) or '(none)'}."
        )


# ---------------------------------------------------------------------------
# move_file
# ---------------------------------------------------------------------------


class MoveFileTool(BaseTool):
    """Move a Library file into a folder."""

    @property
    def name(self) -> str:
        return "move_file"

    @property
    def description(self) -> str:
        return (
            "Move a file in the workspace Library into a folder. 'folder_path' is "
            "an absolute path like '/reports/2026' (use '/' for the root). Operates "
            "only on files in the current workspace."
        )

    @property
    def trust_level(self) -> str:
        return "medium"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "ID of the Library file to move.",
                },
                "folder_path": {
                    "type": "string",
                    "description": "Absolute destination folder path (e.g. '/reports'). "
                    "'/' is the root.",
                },
            },
            "required": ["file_id", "folder_path"],
        }

    async def execute(self, file_id: str, folder_path: str) -> str:
        workspace = _current_workspace()
        if not workspace:
            return self._error("Library verbs require a workspace context (cloud chat session).")

        store = _mongo_store()
        if store is None:
            return self._error("Library is not available (enterprise feature).")

        try:
            from pocketpaw_ee.cloud.uploads.paths import normalize_path

            norm = normalize_path(folder_path)
        except ValueError as e:
            return self._error(f"Invalid folder path: {e}")
        except ImportError:
            return self._error("Library is not available (enterprise feature).")

        doc = await store.get_doc_scoped(file_id, workspace)
        if doc is None:
            return self._error(f"File {file_id!r} not found in this workspace.")

        # Validate the destination folder exists (except root). Reuses the same
        # FolderStore the PATCH route checks against.
        if norm != "/":
            try:
                from pocketpaw_ee.cloud.uploads.folder_store import FolderStore

                if not await FolderStore().path_exists(workspace, norm):
                    return self._error(f"Destination folder {norm!r} does not exist.")
            except ImportError:
                pass

        doc.folder_path = norm
        await doc.save()

        await _emit_library_event(
            "file.moved",
            {
                "file_id": file_id,
                "workspace_id": workspace,
                "folder_path": norm,
                "actor": _current_user() or "agent",
            },
        )
        return self._success(f"Moved {doc.filename} ({file_id}) to {norm}.")


# ---------------------------------------------------------------------------
# annotate_file
# ---------------------------------------------------------------------------


class AnnotateFileTool(BaseTool):
    """Prepend a short note to a Library file's content (a revertable version)."""

    @property
    def name(self) -> str:
        return "annotate_file"

    @property
    def description(self) -> str:
        return (
            "Prepend a short note to a text file's content in the workspace Library "
            "(e.g. a one-line summary). This is a content change: it writes a new "
            "file version, so the annotation is fully revertable. Only text files "
            "can be annotated. Operates only on files in the current workspace."
        )

    @property
    def trust_level(self) -> str:
        return "medium"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "ID of the Library file to annotate.",
                },
                "note": {
                    "type": "string",
                    "description": "The note to prepend (a short line or summary).",
                },
            },
            "required": ["file_id", "note"],
        }

    async def execute(self, file_id: str, note: str) -> str:
        workspace = _current_workspace()
        if not workspace:
            return self._error("Library verbs require a workspace context (cloud chat session).")

        try:
            from pocketpaw_ee.cloud._core.errors import CloudError, NotFound
            from pocketpaw_ee.cloud.file_versions import service as fv_service
        except ImportError:
            return self._error("Library versioning is not available (enterprise feature).")

        ctx = _request_ctx(workspace, _current_user())
        try:
            result = await fv_service.annotate_upload(ctx, file_id, note)
        except NotFound:
            return self._error(f"File {file_id!r} not found in this workspace.")
        except CloudError as e:
            return self._error(str(e))

        return self._success(
            f"Annotated {file_id} — new version {result.new_version} (revertable)."
        )


# ---------------------------------------------------------------------------
# search_library
# ---------------------------------------------------------------------------


class SearchLibraryTool(BaseTool):
    """BM25 search over the workspace's files (captions + extracted text)."""

    @property
    def name(self) -> str:
        return "search_library"

    @property
    def description(self) -> str:
        return (
            "Search the workspace Library for files by content. Runs a keyword "
            "(BM25) search over the text and captions extracted from uploaded files "
            "and returns the ranked hits with snippets. Use to find a file when you "
            "don't know its id (e.g. 'the whiteboard photo from the offsite')."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max hits to return (default 5).",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, limit: int = 5) -> str:
        workspace = _current_workspace()
        if not workspace:
            return self._error("Library verbs require a workspace context (cloud chat session).")

        if not query or not query.strip():
            return self._error("Provide a search query.")

        try:
            from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService
        except ImportError:
            return self._error("Library search is not available (enterprise feature).")

        # Files ingest into the workspace KB scope (workspace:{id}) — the same
        # scope the FileReady listener targets for workspace uploads.
        scope = f"workspace:{workspace}"
        capped = max(1, min(int(limit), 20))
        try:
            context = await KnowledgeService.search_context_for_scope(
                scope=scope, query=query, limit=capped
            )
        except Exception as e:
            logger.warning("search_library failed for %s: %s", scope, e)
            return self._error(f"Search failed: {e}")

        if not context or not context.strip():
            return self._success(f"No Library files matched {query!r}.")
        return self._success(context)
