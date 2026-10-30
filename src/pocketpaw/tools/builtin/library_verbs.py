# library_verbs.py — agent Library verbs (FL-3 + FL-4): tag / move / annotate /
#   search / organize_folder.
# Created: 2026-07-03 (FL-3) — the single-file verbs that make /files agentic.
# Updated: 2026-07-03 (FL-4) — added ``organize_folder``, the folder-batch
#   executor (the Poly headline demo: "add a one-line summary to every file in
#   this folder"). It fans FL-3's single-file verbs (``annotate`` | ``tag``) over
#   every file in a folder as SEPARATE metered invocations — it reuses the FL-3
#   tool internals (``AnnotateFileTool`` / ``TagFileTool``), it does NOT duplicate
#   their logic. Enumeration is workspace-scoped via ``MongoFileStore.iter_by_workspace``
#   filtered on ``folder_path``. A hard cap (``max_files``, default 200) bounds the
#   sweep; over-cap returns a clear count-vs-cap message (never a silent
#   truncation); the default cap is only raised for an admin scope. Partial
#   failures are isolated — one file's error is recorded and the batch continues.
#   Four BaseTool subclasses the agent uses to organize + read the workspace
#   Library:
#     - tag_file(file_id, add?, remove?)  → metadata op, journal event
#     - move_file(file_id, folder_path)   → metadata op, journal event
#     - annotate_file(file_id, note)      → CONTENT op → new FL-2 version (revertable)
#     - search_library(query, limit)      → BM25 search over the workspace KB scope
#   plus the FL-4 batch verb:
#     - organize_folder(folder_path, action, ...) → fans a verb over a folder
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


def _current_is_admin() -> bool:
    """True when the active agent stream runs under an admin scope (FL-4).

    Only an admin scope may raise ``organize_folder``'s ``max_files`` above the
    default cap. The signal is read from ``agent_service.current_is_admin`` when
    that getter exists; absent it (older EE build, or a community install), the
    answer is a conservative ``False`` so the default cap always holds. Matches
    the lazy-import / degrade-gracefully pattern the other ``_current_*`` getters
    use.
    """
    try:
        from pocketpaw_ee.cloud.chat import agent_service

        getter = getattr(agent_service, "current_is_admin", None)
        if getter is None:
            return False
        return bool(getter())
    except ImportError:
        return False
    except Exception:  # pragma: no cover — a broken getter must not gate the verb
        return False


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


# ---------------------------------------------------------------------------
# organize_folder  (FL-4 — the folder-batch executor)
# ---------------------------------------------------------------------------

# The default hard cap on a single batch. A folder larger than this returns a
# clear over-cap message rather than a silent truncation. Only an admin scope
# (``_current_is_admin``) may raise ``max_files`` above this via the param.
_DEFAULT_MAX_FILES = 200
# Absolute ceiling — even an admin scope cannot raise ``max_files`` above this,
# so no single batch can fan an unbounded metered sweep (review F4).
_ABSOLUTE_MAX_FILES = 2000


class OrganizeFolderTool(BaseTool):
    """Apply an FL-3 verb (annotate | tag) to every file in a folder.

    This is the folder-batch executor: it enumerates the workspace files in one
    folder and fans the chosen single-file verb over each as a SEPARATE, metered
    tool invocation (the same ``AnnotateFileTool`` / ``TagFileTool`` the agent
    calls one-off), bounded by a hard cap and with per-file failure isolation.
    """

    @property
    def name(self) -> str:
        return "organize_folder"

    @property
    def description(self) -> str:
        return (
            "Apply the same action to every file in a Library folder — the batch "
            "version of the single-file verbs. 'action' is 'annotate' (prepend a "
            "one-line note to each text file as a revertable version) or 'tag' (add "
            "tags to each file). For 'annotate' pass 'note'; for 'tag' pass 'add' "
            "and/or 'remove'. The sweep is capped at "
            f"{_DEFAULT_MAX_FILES} files (a larger folder is refused, not "
            "truncated), runs only over the current workspace, and reports a "
            "per-file result — one file failing does not abort the rest."
        )

    @property
    def trust_level(self) -> str:
        # Mutating, not auto-approved — matches the FL-3 single-file verbs it fans out.
        return "medium"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "folder_path": {
                    "type": "string",
                    "description": "Absolute folder to sweep (e.g. '/reports'). '/' is the root.",
                },
                "action": {
                    "type": "string",
                    "enum": ["annotate", "tag"],
                    "description": "The per-file verb to apply: 'annotate' or 'tag'.",
                },
                "note": {
                    "type": "string",
                    "description": "For action='annotate': the note to prepend to each file.",
                },
                "add": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For action='tag': tags to add to each file.",
                },
                "remove": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For action='tag': tags to remove from each file.",
                },
                "max_files": {
                    "type": "integer",
                    "description": "Max files to process (default "
                    f"{_DEFAULT_MAX_FILES}). Raising it above the default requires "
                    "an admin scope; otherwise it is clamped to the default.",
                },
            },
            "required": ["folder_path", "action"],
        }

    async def execute(
        self,
        folder_path: str,
        action: str,
        note: str | None = None,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        max_files: int | None = None,
    ) -> str:
        workspace = _current_workspace()
        if not workspace:
            return self._error("Library verbs require a workspace context (cloud chat session).")

        store = _mongo_store()
        if store is None:
            return self._error("Library is not available (enterprise feature).")

        action = (action or "").strip().lower()
        if action not in ("annotate", "tag"):
            return self._error("action must be 'annotate' or 'tag'.")

        # Validate the inner verb's own required args up front so we fail fast
        # before enumerating (and don't half-run a batch of no-op calls).
        if action == "annotate":
            if not note or not note.strip():
                return self._error("action='annotate' requires a non-empty 'note'.")
        else:  # tag
            if not add and not remove:
                return self._error("action='tag' requires at least one tag in 'add' or 'remove'.")

        # Normalize the folder path the same way move_file / the PATCH route do.
        try:
            from pocketpaw_ee.cloud.uploads.paths import normalize_path

            norm = normalize_path(folder_path)
        except ValueError as e:
            return self._error(f"Invalid folder path: {e}")
        except ImportError:
            return self._error("Library is not available (enterprise feature).")

        # Resolve the effective cap. The default always holds for a normal scope;
        # only an admin scope may raise it via ``max_files``.
        effective_cap = _DEFAULT_MAX_FILES
        if max_files is not None:
            try:
                requested = int(max_files)
            except (TypeError, ValueError):
                return self._error("max_files must be an integer.")
            if requested < 1:
                return self._error("max_files must be at least 1.")
            if requested > _DEFAULT_MAX_FILES and not _current_is_admin():
                return self._error(
                    f"max_files={requested} exceeds the default cap of "
                    f"{_DEFAULT_MAX_FILES}; only an admin scope may raise it."
                )
            # Clamp even an admin's request to the absolute ceiling.
            effective_cap = min(requested, _ABSOLUTE_MAX_FILES)

        # Enumerate the workspace's files, filter to this folder. iter_by_workspace
        # is workspace-pinned, so cross-workspace files can never enter the batch.
        # We read up to (cap + 1) matching rows so we can DETECT over-cap without
        # loading an unbounded folder into memory.
        matches: list[dict[str, Any]] = []
        over_cap = False
        async for row in store.iter_by_workspace(workspace, limit=10000):
            fp = (row.get("folder_path") or "/") or "/"
            if fp != norm:
                continue
            if len(matches) >= effective_cap:
                over_cap = True
                break
            matches.append(row)

        if over_cap:
            return self._error(
                f"Folder {norm!r} has more than {effective_cap} files, which "
                f"exceeds the batch cap of {effective_cap}. Narrow the folder or "
                "ask an admin to raise the cap — the batch was not run to avoid "
                "silently processing only part of the folder."
            )

        if not matches:
            return self._success(f"No files in {norm!r} to {action}.")

        # Fan the FL-3 verb over each file as a separate, metered invocation.
        # Reuse the FL-3 tool instances rather than re-implementing their logic.
        annotate_tool = AnnotateFileTool() if action == "annotate" else None
        tag_tool = TagFileTool() if action == "tag" else None

        ok: list[str] = []
        failed: list[tuple[str, str]] = []
        for row in matches:
            file_id = row.get("file_id")
            filename = row.get("filename") or file_id
            if not file_id:
                continue
            try:
                if action == "annotate":
                    result = await annotate_tool.execute(file_id, note)  # type: ignore[arg-type]
                else:
                    result = await tag_tool.execute(file_id, add=add, remove=remove)  # type: ignore[union-attr]
            except Exception as e:  # one file's crash must not abort the batch
                logger.warning("organize_folder: %s on %s crashed: %s", action, file_id, e)
                failed.append((str(filename), f"error: {e}"))
                continue

            # The inner verbs return a string that starts with a failure marker on
            # error (BaseTool._error) — treat that as a per-file failure, not a stop.
            if _looks_like_error(result):
                failed.append((str(filename), _strip_marker(result)))
            else:
                ok.append(str(filename))

        await _emit_library_event(
            "folder.batch",
            {
                "workspace_id": workspace,
                "folder_path": norm,
                "action": action,
                "ok": len(ok),
                "failed": len(failed),
                "actor": _current_user() or "agent",
            },
        )

        summary = [
            f"Batch {action} on {norm}: {len(ok)} succeeded, {len(failed)} failed "
            f"(of {len(matches)} files)."
        ]
        if ok:
            summary.append("Succeeded: " + ", ".join(ok))
        if failed:
            summary.append("Failed: " + "; ".join(f"{name} ({reason})" for name, reason in failed))
        return self._success("\n".join(summary))


def _looks_like_error(result: str) -> bool:
    """Best-effort: does an inner-verb return string signal a failure?

    ``BaseTool._error`` prefixes a stable marker; if that marker moves this
    degrades to treating everything as success, which is why the batch ALSO
    catches raised exceptions above — the two guards together keep partial
    failures isolated.
    """
    if not result:
        return False
    # ``BaseTool._error`` formats as ``"Error: <message>"``.
    return result.strip().lower().startswith("error")


def _strip_marker(result: str) -> str:
    """Trim the leading ``Error:`` marker so the per-file reason reads cleanly."""
    text = (result or "").strip()
    for marker in ("Error:", "error:"):
        if text.startswith(marker):
            return text[len(marker) :].strip()
    return text
