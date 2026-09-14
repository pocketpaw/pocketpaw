# files.py — in-process MCP server exposing the workspace's UPLOADED FILES
# (list + read) to the claude_agent_sdk cloud chat backend.
#
# Created: 2026-09-14 (fix/attachment-not-on-disk).
#
# WHY THIS EXISTS. Uploads live in object storage (S3), and until now the agent
# had no tool that could reach one. The only path was PUSH:
# ``_build_attachments_block`` inlines the extracted text of the files attached
# to THIS turn into the system prompt. That leaves two holes, and the second is
# the reported bug:
#
#   1. attachments are per-turn. ``load_history_for_scope`` rehydrates history
#      as ``[{role, content}]`` only, so nothing carries a file forward. Ask
#      about a document two turns after uploading it and there is no
#      <uploaded-files> block, because ``attachments_in`` is empty for that turn.
#   2. the backend that could go looking has the wrong map. ``pydantic_ai`` is
#      dispatch-only, so the inlined text is the only copy it can reach and it
#      reads it. ``claude_sdk`` grants Bash/Read/Write/Edit/Glob/Grep against a
#      local ``cwd`` jail (``_resolve_cwd``) — and the files are in S3, not in
#      that jail. So it globbed an empty directory and told the user there was
#      no file. On turn 3 that answer was CORRECT: it had neither the text nor a
#      tool.
#
# So give it the tool. ``list_uploads`` answers "what has been uploaded here"
# and ``read_upload`` answers "give me that one's contents", both
# workspace-scoped.
#
# PRIVACY. ``hide_from_ai`` is a per-file owner toggle that opts a file out of
# every AI path, and this server is an AI path. Both tools honour it:
#   * listing filters in Mongo as ``{"$ne": True}``, NOT ``== False`` — rows
#     predating the flag have no such key and the equality form silently drops
#     every legacy row (the reasoning is documented on
#     ``MongoFileStore.list_by_kb_articles``);
#   * reading goes through ``load_extracted_text``, which the uploads module
#     documents as the door the privacy gate lives on. Its ``None`` means
#     "extract it yourself", so the live-extraction fallback below re-checks
#     ``hide_from_ai`` explicitly — without that the fallback would walk around
#     the gate the stored-text path just enforced.
#
# TENANCY. The workspace comes from the per-stream ContextVar (``_identity``),
# never from tool args — an agent-supplied workspace id would be a cross-tenant
# read primitive. Every store call is the ``*_scoped`` variant.
#
# EE→OSS boundary + shape: clones stock_images.py — ``create_sdk_mcp_server``
# behind an SDK import-guard, ``SERVER_NAME`` / ``*_TOOL_ID`` allowlist
# constants, ``(name, server) | None`` from the builder so the backend's MCP
# registration loop treats it like every other in-process server.
"""Agent-side MCP surface for reading the workspace's uploaded files."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_files"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
LIST_UPLOADS_TOOL_ID = f"mcp__{SERVER_NAME}__list_uploads"
READ_UPLOAD_TOOL_ID = f"mcp__{SERVER_NAME}__read_upload"

FILES_TOOL_IDS = (LIST_UPLOADS_TOOL_ID, READ_UPLOAD_TOOL_ID)

# One file's text, capped the way the inline attachment path caps it
# (``_ATTACHMENT_PER_FILE_CHARS``). A tool result joins the same context window,
# so the same ceiling applies; the agent can ask for another file if it needs
# more. Truncation is ANNOUNCED — silent truncation is how an agent ends up
# reporting that the USER truncated something.
_READ_CHARS_CAP = 40000
_LIST_DEFAULT_LIMIT = 50
_LIST_MAX_LIMIT = 200


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: Any) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None]:
    """Resolve the active workspace + user id from the per-stream ContextVars set
    by the cloud chat agent runtime. Returns ``(workspace_id, user_id)``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        return None, None


def _store() -> Any | None:
    """The Mongo-backed file metadata store, or ``None`` if unavailable."""
    try:
        from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

        return MongoFileStore()
    except Exception:  # noqa: BLE001
        logger.debug("files: MongoFileStore unavailable", exc_info=True)
        return None


async def _list_handler(args: dict) -> dict:
    """MCP handler for ``files__list_uploads``.

    Workspace-scoped, newest first, ``hide_from_ai`` rows excluded. ``query`` is
    an optional case-insensitive substring matched against filename/summary/tags
    — the agent usually knows what the user CALLED the file, not its id.
    """
    workspace_id, _ = _identity()
    if not workspace_id:
        return _error_response("no active workspace on this run; list_uploads is unavailable here.")

    store = _store()
    if store is None:
        return _error_response("the file store is unavailable.")

    limit = args.get("limit")
    if not isinstance(limit, int) or limit < 1:
        limit = _LIST_DEFAULT_LIMIT
    limit = min(limit, _LIST_MAX_LIMIT)

    try:
        records = await store.list_by_workspace(workspace_id, limit=limit, ai_visible_only=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("files: list failed", exc_info=True)
        return _error_response(f"listing uploads failed: {exc}")

    query = args.get("query")
    needle = query.strip().casefold() if isinstance(query, str) and query.strip() else None

    out: list[dict[str, Any]] = []
    for rec in records:
        filename = getattr(rec, "filename", "") or ""
        summary = getattr(rec, "summary", None) or ""
        tags = list(getattr(rec, "tags", []) or [])
        if needle:
            haystack = " ".join([filename, summary, " ".join(tags)]).casefold()
            if needle not in haystack:
                continue
        out.append(
            {
                "file_id": getattr(rec, "id", None),
                "filename": filename,
                "mime": getattr(rec, "mime", None),
                "size": getattr(rec, "size", None),
                "summary": summary or None,
                "tags": tags,
                "created": getattr(rec, "created", None),
            }
        )

    return _success_response({"ok": True, "count": len(out), "files": out})


async def _read_handler(args: dict) -> dict:
    """MCP handler for ``files__read_upload``.

    Prefers the PERSISTED extraction (``load_extracted_text``), which is also
    where the ``hide_from_ai`` gate and the stale-``content_version`` check live.
    Its ``None`` means "extract it yourself", so we fall back to a live
    extraction — after re-checking ``hide_from_ai``, because that fallback would
    otherwise route around the gate that just refused.
    """
    file_id = args.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return _error_response("read_upload requires a non-empty `file_id`.")
    file_id = file_id.strip()

    workspace_id, _ = _identity()
    if not workspace_id:
        return _error_response("no active workspace on this run; read_upload is unavailable here.")

    store = _store()
    if store is None:
        return _error_response("the file store is unavailable.")

    # The /files preamble renders ids as an 8-char TAIL (``prompt.entity.
    # short_id``), so the id the agent was SHOWN is not the id Mongo stores.
    # Resolve it the one sanctioned way: ``pockets.id_resolve.resolve_id``,
    # against a candidate list WE scope to this workspace. It strips the tail
    # marker, and an ambiguous tail RAISES rather than picking — a loose match
    # here would open the wrong document while looking like it worked. A whole
    # id passes straight through, so the common path is unchanged.
    try:
        file_id = await _resolve_file_id(store, file_id, workspace_id)
    except _Ambiguous as exc:
        return _error_response(str(exc))

    try:
        doc = await store.get_doc_scoped(file_id, workspace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("files: lookup failed for %s", file_id, exc_info=True)
        return _error_response(f"looking up {file_id} failed: {exc}")

    # A workspace-scoped miss reads as "not found" — never as "exists elsewhere".
    if doc is None:
        return _error_response(
            f"no file {file_id} in this workspace. Call list_uploads to see what is here."
        )

    filename = getattr(doc, "filename", None) or "upload"
    mime = getattr(doc, "mime", None) or "application/octet-stream"

    if getattr(doc, "hide_from_ai", False):
        return _error_response(
            f"{filename} is marked hidden from AI by its owner, so its contents "
            "cannot be read. Tell the user that rather than guessing at it."
        )

    text: str | None = None
    try:
        from pocketpaw_ee.cloud.uploads.extracted_text import load_extracted_text

        stored = await load_extracted_text(doc)
        if stored is not None:
            text = (getattr(stored, "text", "") or "").strip()
    except Exception:  # noqa: BLE001
        logger.debug("files: stored extraction unavailable for %s", file_id, exc_info=True)

    if not text:
        text = await _live_extract(file_id, workspace_id)

    if not text:
        return _success_response(
            {
                "ok": True,
                "file_id": file_id,
                "filename": filename,
                "mime": mime,
                "text": None,
                "note": (
                    "No text could be extracted from this file. It is likely an "
                    "image, video or other binary. Do not invent its contents."
                ),
            }
        )

    truncated = len(text) > _READ_CHARS_CAP
    if truncated:
        text = text[:_READ_CHARS_CAP].rstrip()

    return _success_response(
        {
            "ok": True,
            "file_id": file_id,
            "filename": filename,
            "mime": mime,
            "text": text,
            "truncated": truncated,
        }
    )


class _Ambiguous(Exception):
    """An id tail that matches more than one file in this workspace."""


async def _resolve_file_id(store: Any, raw: str, workspace_id: str) -> str:
    """Turn the id the prompt SHOWED into the id Mongo stores.

    Whole ids pass through untouched — the resolver only has work to do for a
    rendered tail, and it is scoped to this workspace's own rows, never to a
    global search (an unscoped tail resolve would be a tenancy hole).
    Resolution failing for any other reason returns the input, so the caller's
    ordinary "not found" path reports it.
    """
    try:
        from pocketpaw_ee.cloud.pockets.id_resolve import AmbiguousId, resolve_id
    except Exception:  # noqa: BLE001
        return raw

    try:
        candidates = await store.list_by_workspace(
            workspace_id, limit=500, ai_visible_only=True
        )
        return resolve_id(raw, list(candidates))
    except AmbiguousId as exc:
        raise _Ambiguous(
            f"{exc} Call list_uploads and pass the full file_id you want."
        ) from exc
    except Exception:  # noqa: BLE001
        logger.debug("files: id resolve fell through for %s", raw, exc_info=True)
        return raw


async def _live_extract(file_id: str, workspace_id: str) -> str | None:
    """Extract text straight from the blob, for a row with no usable stored text.

    Mirrors ``_build_attachments_block``'s resolve-then-chain path. Callers MUST
    have cleared ``hide_from_ai`` first — this function does not re-check it.
    """
    try:
        from pocketpaw.config import get_settings
        from pocketpaw_ee.cloud.extraction import build_chain
        from pocketpaw_ee.cloud.uploads.resolver import default_resolver
    except Exception:  # noqa: BLE001
        logger.debug("files: extraction deps unavailable", exc_info=True)
        return None

    try:
        resolver = default_resolver()
        cm = resolver.open_local_for_url(f"upload://{file_id}", workspace=workspace_id)
        async with cm as resolved:
            if resolved is None:
                return None
            rec, path = resolved
            chain = build_chain(get_settings())
            result = await chain.run(path, rec.mime)
            return (getattr(result, "text", "") or "").strip() or None
    except Exception:  # noqa: BLE001
        logger.warning("files: live extraction failed for %s", file_id, exc_info=True)
        return None


def build_files_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for uploaded files, or ``None`` if the
    Claude Agent SDK isn't installed. Matches the ``(name, server)`` / ``None``
    shape of ``build_media_server``."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_files MCP disabled")
        return None

    @tool(
        "list_uploads",
        (
            "List the files the user has uploaded to this workspace, newest "
            "first. Use this whenever the user refers to a document, file, "
            "upload, PDF, image or 'the thing I sent' that is NOT inlined in the "
            "<uploaded-files> block of your context — attachments are inlined "
            "only for the turn they were attached to, so anything uploaded "
            "earlier in the conversation must be found here. Args: optional "
            "`query` (case-insensitive substring matched against filename, "
            "summary and tags) and `limit` (default 50, max 200). Returns {ok, "
            "count, files:[{file_id, filename, mime, size, summary, tags, "
            "created}]}. Pass a file_id to read_upload to get its contents. An "
            "empty list means nothing is uploaded here — say so, do not guess."
        ),
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring to match against filename, summary and tags.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many files to return (default 50, max 200).",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    )
    async def list_uploads_tool(args):  # type: ignore[no-untyped-def]
        return await _list_handler(args)

    @tool(
        "read_upload",
        (
            "Read the extracted text of one uploaded file by its `file_id` (get "
            "ids from list_uploads). Use this to answer questions about a "
            "document the user uploaded earlier in the conversation. Uploaded "
            "files live in object storage, NOT on your local filesystem — Read, "
            "Glob, Grep and Bash cannot see them, so this tool is the only way "
            "to reach one. Returns {ok, file_id, filename, mime, text, "
            "truncated}. `text` is null for images, video and other binaries "
            "with no extractable text — report that rather than inventing "
            "contents. A file its owner marked hidden from AI returns an error; "
            "tell the user instead of working around it."
        ),
        {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The file's id, as returned by list_uploads.",
                },
            },
            "required": ["file_id"],
            "additionalProperties": False,
        },
    )
    async def read_upload_tool(args):  # type: ignore[no-untyped-def]
        return await _read_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[list_uploads_tool, read_upload_tool],
    )
    return SERVER_NAME, server


__all__ = [
    "FILES_TOOL_IDS",
    "LIST_UPLOADS_TOOL_ID",
    "READ_UPLOAD_TOOL_ID",
    "SERVER_NAME",
    "build_files_server",
]
