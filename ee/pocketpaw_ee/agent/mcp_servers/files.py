# files.py — in-process MCP server exposing the workspace's UPLOADED FILES
# (list + read) to the claude_agent_sdk cloud chat backend.
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
# SOURCE FILES. HTML, JS, CSS and SVG skip the stored extraction (a visible-text
# pass that keeps only a script-rendered page's title) and are read from the
# blob as source, inline data stripped, paged by ``offset``, searched by ``query``.
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

import asyncio
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
# ``read_upload(query=...)``: a plain substring search over the same text
# ``offset`` pages, so each match offset is a valid read offset.
_QUERY_MAX_CHARS = 200
_QUERY_MAX_MATCHES = 20
_QUERY_CONTEXT_CHARS = 300


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

    Code-like files skip the stored text and are read as source (see
    ``_live_source``). ``offset`` pages through any text past the read cap;
    ``query`` instead returns match offsets (searching from ``offset``).
    """
    file_id = args.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return _error_response("read_upload requires a non-empty `file_id`.")
    file_id = file_id.strip()

    offset = args.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        offset = 0

    query = args.get("query")
    query = query.strip() if isinstance(query, str) else ""
    if len(query) > _QUERY_MAX_CHARS:
        return _error_response(f"`query` is limited to {_QUERY_MAX_CHARS} characters.")

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

    try:
        from pocketpaw_ee.cloud.extraction.local import is_source_file

        as_source = is_source_file(filename, mime)
    except Exception:  # noqa: BLE001
        as_source = False

    text: str | None = None
    source = await _live_source(file_id, workspace_id) if as_source else None
    if source is not None:
        text = source.text
    else:
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

    # Pages are not stripped: whitespace at a page edge belongs to the file.
    total = len(text)
    end = offset + _READ_CHARS_CAP
    more = end < total
    body: dict[str, Any] = {
        "ok": True,
        "file_id": file_id,
        "filename": filename,
        "mime": mime,
        "total_chars": total,
        "content_dropped": bool(source and (source.stripped_chars or source.truncated)),
    }
    if query:
        matches, total_matches, next_offset = _find_matches(text, query, offset)
        body.update(query=query, total_matches=total_matches, matches=matches)
    else:
        body.update(
            {
                "text": text[offset:end],
                "truncated": more or bool(source and source.truncated),
                "offset": offset,
            }
        )
    notes: list[str] = []
    # A text-like file that yields under a tenth of its bytes as text lost the
    # rest somewhere (a script-rendered page's title, say). A source read already
    # accounts for what it dropped (stripped blobs, the read limit) below.
    size = getattr(doc, "size", None)
    if (
        source is None
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size > 0
        and _is_text_like(mime, as_source)
        and total < size // 10
    ):
        body["content_dropped"] = True
        notes.append(
            f"Only {total} of the file's {size} bytes came through as text; the rest "
            "was not extracted. Do not assume the missing part is empty."
        )
    if source is not None and (source.stripped_chars or source.truncated):
        body["stripped_chars"] = source.stripped_chars
        if source.stripped_chars:
            notes.append(
                "Inline data (images, fonts) was replaced by [N chars stripped] "
                "markers; the markup, scripts and styles are intact."
            )
        if source.truncated:
            notes.append("The file is larger than the read limit; only its start was read.")
    if query:
        if not matches:
            where = f" after offset {offset}" if offset else ""
            notes.append(f"No match for {query!r}{where}.")
        else:
            if next_offset is not None:
                body["next_offset"] = next_offset
                notes.append(
                    f"Showing {len(matches)} of {total_matches} matches. Call read_upload "
                    f"with file_id={file_id}, query={query!r} and offset={next_offset} "
                    "for the next ones."
                )
            notes.append(
                f"Read around a match with read_upload(file_id={file_id}, offset=<match offset>)."
            )
    elif more:
        body["next_offset"] = end
        notes.append(
            f"Call read_upload again with file_id={file_id} and offset={end} to read on; "
            "keep going until truncated is false before concluding anything is missing. "
            "Or pass query=<words> to jump straight to a section, then read at the match offset."
        )
    if notes:
        body["note"] = " ".join(notes)

    return _success_response(body)


def _find_matches(
    text: str, query: str, start: int
) -> tuple[list[dict[str, Any]], int, int | None]:
    """Case-insensitive, non-overlapping substring matches of ``query`` in
    ``text`` from ``start``: ``(first _QUERY_MAX_MATCHES matches, total count,
    offset just past the last returned match if more remain, else None)``."""
    lowered = text.lower()
    # str.lower() changes length for a few non-ASCII chars; offsets would drift, so match exactly.
    hay, needle = (lowered, query.lower()) if len(lowered) == len(text) else (text, query)
    matches: list[dict[str, Any]] = []
    total = 0
    last_end = start
    i = hay.find(needle, start)
    while i != -1:
        total += 1
        if total <= _QUERY_MAX_MATCHES:
            lo, hi = max(0, i - _QUERY_CONTEXT_CHARS), i + len(needle) + _QUERY_CONTEXT_CHARS
            matches.append({"offset": i, "snippet": text[lo:hi]})
            last_end = i + len(needle)
        i = hay.find(needle, i + len(needle))
    return matches, total, (last_end if total > _QUERY_MAX_MATCHES else None)


def _is_text_like(mime: str, as_source: bool) -> bool:
    """Whether a file's bytes are mostly text, so its size bounds its text."""
    m = mime.split(";", 1)[0].strip().lower()
    # Exact types and +json/+xml suffixes only: every OOXML (docx/xlsx/pptx)
    # mime contains "xml" and is a zip, not text.
    return (
        as_source
        or m.startswith("text/")
        or m in ("application/json", "application/xml")
        or m.endswith(("+json", "+xml"))
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
        candidates = await store.list_by_workspace(workspace_id, limit=500, ai_visible_only=True)
        return resolve_id(raw, list(candidates))
    except AmbiguousId as exc:
        raise _Ambiguous(f"{exc} Call list_uploads and pass the full file_id you want.") from exc
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
        cm = default_resolver().open_local_for_id(file_id, workspace=workspace_id)
        async with cm as resolved:
            if resolved is None:
                logger.info("files: no blob for %s in workspace %s", file_id, workspace_id)
                return None
            rec, path = resolved
            chain = build_chain(get_settings())
            result = await chain.run(path, rec.mime)
            return (getattr(result, "text", "") or "").strip() or None
    except Exception:  # noqa: BLE001
        logger.warning("files: live extraction failed for %s", file_id, exc_info=True)
        return None


async def _live_source(file_id: str, workspace_id: str) -> Any | None:
    """Read a code-like upload straight from the blob as ``SourceText``.

    Same resolve path as ``_live_extract``; ``None`` on any failure, so the
    caller falls back to the stored extraction. Callers MUST have cleared
    ``hide_from_ai`` first.
    """
    try:
        from pocketpaw_ee.cloud.extraction.local import read_source
        from pocketpaw_ee.cloud.uploads.resolver import default_resolver

        cm = default_resolver().open_local_for_id(file_id, workspace=workspace_id)
        async with cm as resolved:
            if resolved is None:
                logger.info("files: no blob for %s in workspace %s", file_id, workspace_id)
                return None
            _rec, path = resolved
            return await asyncio.to_thread(read_source, path)
    except Exception:  # noqa: BLE001
        logger.warning("files: source read failed for %s", file_id, exc_info=True)
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
            "truncated, offset, total_chars, next_offset?, content_dropped}. "
            "Code-like files (HTML, JS, CSS, SVG) come back as source with "
            "inline data and base64 stripped. `content_dropped` true means the "
            "text is not the whole file. Always page: when `next_offset` is "
            "present, call again with it as `offset`, and never conclude a file "
            "lacks something until `truncated` is false. To find a section of a "
            "long file, pass `query` (case-insensitive substring, not regex): it "
            "returns {total_matches, matches:[{offset, snippet}]} instead of text; "
            "read at a match's `offset`, and page matches with `next_offset`. "
            "`text` is null for images, "
            "video and other binaries with no extractable text — report that "
            "rather than inventing contents. A file its owner marked hidden from "
            "AI returns an error; tell the user instead of working around it."
        ),
        {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The file's id, as returned by list_uploads.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Character offset to start reading at (default 0).",
                },
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": (
                        "Case-insensitive substring (not regex) to search for from `offset`; "
                        "returns match offsets instead of text."
                    ),
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
