# router.py — FastAPI router for the file-version write API.
# Created: 2026-06-26 (ART-1) — ported from dewani12's origin/feature/files
#   (ee/cloud/file_versions/router.py). KEEPS the 4 core routes: POST
#   /files/write, PUT /files/{id}, GET /files/{id}/versions,
#   GET /files/{id}/versions/{vid}. DROPPED the Slice-D editor routes
#   (revert, diff, ai-edit, editing/spreadsheet/slides-context). Thin
#   pass-through: ``Depends(request_context)``, no Beanie, no HTTPException —
#   the service raises CloudError subclasses and ``_core.http`` maps them to
#   JSON. Coexists with the existing /files router (GET /files, /tree,
#   /browse) — no method+path collisions.
# Updated: 2026-07-03 (FL-2, port of #1193) — restored the two history routes
#   ART-1 deferred: POST /files/{id}/versions/{vid}/revert (restore a prior
#   version) and GET /files/{id}/versions/{v1}/diff/{v2} (unified diff). The
#   AI-edit / editing-context routes remain deferred (they pull in FL-5 tool
#   deps). Doc note: a stale If-Match now maps to 412 (PreconditionFailed),
#   not 409.
# Updated: 2026-07-03 (FL-5, port of #1193) — restored the AI-edit + context-sync
#   editor routes ART-1/FL-2 deferred, now that the FL-5 edit tools land:
#     - POST /files/{id}/ai-edit            (document AI edit)
#     - PUT/GET /files/{id}/editing-context (sync/read Editor.js blocks)
#     - POST /files/{id}/spreadsheet-edit   (workbook AI edit)
#     - PUT/GET /files/{id}/spreadsheet-context
#     - POST /files/{id}/slides-edit        (deck AI edit)
#     - PUT/GET /files/{id}/slides-context
#   The *-edit routes run the workspace's default agent inline via
#   ``pocketpaw.agents.pool`` with the edit tool's in-memory session-store bound,
#   so the ``edit_*`` MCP tool mutates the parsed blocks/deck/snapshot; the final
#   state returns to the frontend, which persists it via ``PUT /files/{id}`` (a
#   versioned write). The *-context routes are the ``editor_blocks`` /
#   spreadsheet / slides transport round-trip: PUT stores the frontend's current
#   state in the module store, GET reads it back (possibly mutated by a
#   chat-initiated edit). These routes read the caller's ``current_user_id`` /
#   ``current_workspace_id`` deps (dewani's #1193 signature) rather than the
#   ``request_context`` the core write routes use — they run the agent, which
#   needs the identity, not a RequestContext.
"""FileVersions router — file-version write + history API."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import BadRequest
from pocketpaw_ee.cloud.file_versions import service as _svc
from pocketpaw_ee.cloud.file_versions.dto import (
    AiEditRequest,
    AiEditResponse,
    DiffResponse,
    FileVersionListItem,
    FileVersionResponse,
    SyncEditingContextRequest,
    UpdateFileContentRequest,
    UpdateFileContentResponse,
    WriteFileRequest,
    WriteFileResponse,
)
from pocketpaw_ee.cloud.file_versions.slides_dto import (
    SlidesEditRequest,
    SlidesEditResponse,
    SyncSlidesContextRequest,
)
from pocketpaw_ee.cloud.file_versions.spreadsheet_dto import (
    SpreadsheetEditRequest,
    SpreadsheetEditResponse,
    SyncSpreadsheetContextRequest,
)
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

router = APIRouter(
    prefix="/files",
    tags=["File Versions"],
    dependencies=[Depends(require_license)],
)


@router.post("/write", status_code=201, response_model=WriteFileResponse)
async def write_file(
    body: WriteFileRequest,
    ctx: RequestContext = Depends(request_context),
) -> WriteFileResponse:
    """Create or overwrite a file. Returns the file id for subsequent versioned saves."""
    return await _svc.write_file(ctx, body)


@router.put("/{file_id}", response_model=UpdateFileContentResponse)
async def update_file(
    file_id: str,
    body: UpdateFileContentRequest,
    ctx: RequestContext = Depends(request_context),
    if_match: str | None = Header(None, alias="If-Match"),
) -> UpdateFileContentResponse:
    """Replace a text file's content inline.

    Body: ``{ content: "<new text>", expectedVersion?: <int> }``.
    Header: ``If-Match: <current content_version>`` (optimistic concurrency)
    takes precedence over ``expectedVersion`` when present.

    Raises (mapped to JSON by ``_core.http``): 404 if the file is missing,
    412 on a stale ``If-Match`` (version conflict), 422 if the file type isn't
    editable.
    """
    # If-Match header (optimistic concurrency) wins over the body field.
    if if_match is not None:
        try:
            body.expected_version = int(if_match.strip('"'))
        except ValueError:
            raise BadRequest("files.bad_version", "If-Match must be an integer.") from None

    return await _svc.update_file_content(ctx, file_id, body)


@router.get("/{file_id}/versions", response_model=list[FileVersionListItem])
async def list_file_versions(
    file_id: str,
    ctx: RequestContext = Depends(request_context),
) -> list[FileVersionListItem]:
    """List all archived versions for a file (oldest first, no content)."""
    return await _svc.list_versions(ctx, file_id)


@router.get("/{file_id}/versions/{version_id}", response_model=FileVersionResponse)
async def get_file_version(
    file_id: str,
    version_id: str,
    ctx: RequestContext = Depends(request_context),
) -> FileVersionResponse:
    """Fetch a single version with full content (for revert preview / diff)."""
    return await _svc.get_version(ctx, file_id, version_id)


@router.post(
    "/{file_id}/versions/{version_id}/revert",
    response_model=UpdateFileContentResponse,
)
async def revert_file_version(
    file_id: str,
    version_id: str,
    ctx: RequestContext = Depends(request_context),
) -> UpdateFileContentResponse:
    """Restore the live file to a historical version.

    Archives the current content as a new version, then writes the target
    version's content as the new live content. Tenant-filtered: a
    cross-workspace ``version_id`` is a 404.
    """
    return await _svc.revert_to_version(ctx, file_id, version_id)


@router.get(
    "/{file_id}/versions/{from_version_id}/diff/{to_version_id}",
    response_model=DiffResponse,
)
async def diff_file_versions(
    file_id: str,
    from_version_id: str,
    to_version_id: str,
    ctx: RequestContext = Depends(request_context),
) -> DiffResponse:
    """Return a unified diff between two archived versions (``from`` -> ``to``).

    Tenant-filtered: both versions are fetched workspace-scoped, so a diff can
    never span a workspace boundary (a cross-workspace id is a 404).
    """
    return await _svc.diff_versions(ctx, file_id, from_version_id, to_version_id)


# ===========================================================================
# AI-edit + context-sync editor routes (FL-5, port of #1193)
#
# These run the workspace's default agent inline via ``pocketpaw.agents.pool``
# with the relevant edit tool's in-memory session store bound, so the ``edit_*``
# MCP tool mutates the parsed blocks/deck/snapshot; the final state returns to the
# frontend (which persists it via ``PUT /files/{id}``, a versioned write). The
# *-context routes are the transport round-trip: PUT stores the frontend's
# current state, GET reads it back.
# ===========================================================================


async def _resolve_default_agent(workspace_id: str) -> str | None:
    """The workspace's default (first) agent id, or None when it has none."""
    from pocketpaw_ee.cloud.agents import service as agents_service

    agents = await agents_service.list_agents(workspace_id)
    return agents[0].id if agents else None


# --- document -------------------------------------------------------------


def _build_edit_system_prompt(
    blocks: list[dict],
    available_tools: list[str],
    selected_block_id: str | None,
    user_prompt: str,
) -> str:
    """Delegate to the canonical prompt builder so chat + REST share the format."""
    from pocketpaw.tools.builtin.edit_document import build_editor_prompt_context

    ctx = build_editor_prompt_context(
        blocks=blocks,
        available_tools=available_tools,
        selected_block_id=selected_block_id,
    )
    if ctx is None:
        ctx = (
            "The document is currently empty (no blocks). "
            "Use the edit_document tool to insert the first blocks."
        )
    return (
        f"{ctx}\n\nThe user's request: {user_prompt}\n\n"
        "After making your edits, briefly tell the user what you changed."
    )


@router.post("/{file_id}/ai-edit")
async def ai_edit_file(
    file_id: str,
    body: AiEditRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Run an AI agent to edit document content.

    Parses Editor.js blocks, stores them in a per-request ContextVar so the
    ``edit_document`` MCP tool can access them, and runs the workspace's default
    agent. Returns the final blocks array to the frontend.
    """
    try:
        doc = json.loads(body.content) if body.content and body.content.strip() else {}
        blocks: list[dict] = doc.get("blocks", []) if isinstance(doc, dict) else []
    except (json.JSONDecodeError, TypeError):
        blocks = []

    # Deep-copy so the ContextVar stores a mutable list independent of the parse.
    blocks = [json.loads(json.dumps(b)) for b in blocks]

    system_prompt = _build_edit_system_prompt(
        blocks=blocks,
        available_tools=body.available_tools,
        selected_block_id=body.selected_block_id,
        user_prompt=body.prompt,
    )

    from pocketpaw.tools.builtin.edit_document import (
        clear_edit_session,
        clear_selected_block_id,
        set_edit_session,
        set_editor_blocks,
        set_selected_block_id,
    )

    set_edit_session(blocks)
    set_editor_blocks(file_id, blocks)
    set_selected_block_id(body.selected_block_id)

    try:
        from pocketpaw.agents.pool import get_agent_pool

        pool = get_agent_pool()
        agent_id = await _resolve_default_agent(workspace_id)
        if not agent_id:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "ai_edit.no_agent",
                    "message": "No agent available in this workspace.",
                },
            )

        session_key = f"cloud:file-edit:{file_id}:{agent_id}"
        summary_parts: list[str] = []

        async for event in pool.run(
            agent_id,
            body.prompt,
            session_key,
            knowledge_context=system_prompt,
        ):
            if event.type == "message":
                summary_parts.append(str(event.content))
            elif event.type == "error":
                return JSONResponse(
                    status_code=500,
                    content={"detail": "ai_edit.agent_error", "message": str(event.content)},
                )
            elif event.type == "done":
                break

        return JSONResponse(
            content=AiEditResponse(
                blocks=blocks,
                summary="".join(summary_parts).strip() or "Document edited.",
            ).model_dump(by_alias=True)
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"detail": "ai_edit.failed", "message": str(exc)},
        )
    finally:
        clear_edit_session()
        clear_selected_block_id()


@router.put("/{file_id}/editing-context")
async def sync_editing_context(
    file_id: str,
    body: SyncEditingContextRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Store the current Editor.js blocks so the chat agent's MCP tool can
    access them during chat-initiated editing."""
    from pocketpaw.tools.builtin.edit_document import set_editor_blocks

    set_editor_blocks(file_id, body.blocks)
    return JSONResponse(status_code=200, content={"ok": True})


@router.get("/{file_id}/editing-context")
async def get_editing_context(
    file_id: str,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Return the current blocks for a file (may have been mutated by the
    edit_document MCP tool during a chat session)."""
    from pocketpaw.tools.builtin.edit_document import get_editor_blocks

    blocks = get_editor_blocks(file_id)
    if blocks is None:
        return JSONResponse(status_code=404, content={"detail": "No editing session active."})
    return JSONResponse(status_code=200, content={"blocks": blocks})


# --- spreadsheet ----------------------------------------------------------


def _build_spreadsheet_system_prompt(
    snapshot: dict,
    selected_sheet: str | None,
    user_prompt: str,
) -> str:
    """Build the system prompt for spreadsheet AI editing."""
    from pocketpaw.tools.builtin.edit_spreadsheet import build_spreadsheet_prompt_context

    ctx = build_spreadsheet_prompt_context(snapshot=snapshot, selected_sheet=selected_sheet)
    if ctx is None:
        ctx = (
            "The workbook is currently empty (no sheets). "
            "Use the edit_spreadsheet tool to create the first sheet and populate it."
        )
    return (
        f"{ctx}\n\nThe user's request: {user_prompt}\n\n"
        "After making your edits, briefly tell the user what you changed."
    )


@router.post("/{file_id}/spreadsheet-edit")
async def spreadsheet_ai_edit(
    file_id: str,
    body: SpreadsheetEditRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Run an AI agent to edit spreadsheet content. Returns the final snapshot."""
    try:
        snapshot = json.loads(body.content) if body.content and body.content.strip() else {}
        if not isinstance(snapshot, dict):
            snapshot = {}
    except (json.JSONDecodeError, TypeError):
        snapshot = {}

    snapshot = json.loads(json.dumps(snapshot))

    system_prompt = _build_spreadsheet_system_prompt(
        snapshot=snapshot,
        selected_sheet=body.selected_sheet,
        user_prompt=body.prompt,
    )

    from pocketpaw.tools.builtin.edit_spreadsheet import (
        clear_edit_session,
        clear_selected_sheet,
        set_edit_session,
        set_selected_sheet,
        set_spreadsheet_snapshot,
    )

    set_edit_session(snapshot)
    set_spreadsheet_snapshot(file_id, snapshot)
    set_selected_sheet(body.selected_sheet)

    try:
        from pocketpaw.agents.pool import get_agent_pool

        pool = get_agent_pool()
        agent_id = await _resolve_default_agent(workspace_id)
        if not agent_id:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "spreadsheet_edit.no_agent",
                    "message": "No agent available in this workspace.",
                },
            )

        session_key = f"cloud:spreadsheet-edit:{file_id}:{agent_id}"
        summary_parts: list[str] = []

        async for event in pool.run(
            agent_id,
            body.prompt,
            session_key,
            knowledge_context=system_prompt,
        ):
            if event.type == "message":
                summary_parts.append(str(event.content))
            elif event.type == "error":
                return JSONResponse(
                    status_code=500,
                    content={"detail": "spreadsheet_edit.agent_error", "message": str(event.content)},
                )
            elif event.type == "done":
                break

        return JSONResponse(
            content=SpreadsheetEditResponse(
                snapshot=snapshot,
                summary="".join(summary_parts).strip() or "Spreadsheet edited.",
            ).model_dump(by_alias=True)
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"detail": "spreadsheet_edit.failed", "message": str(exc)},
        )
    finally:
        clear_edit_session()
        clear_selected_sheet()


@router.put("/{file_id}/spreadsheet-context")
async def sync_spreadsheet_context(
    file_id: str,
    body: SyncSpreadsheetContextRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Store the current workbook snapshot for chat-initiated spreadsheet editing."""
    from pocketpaw.tools.builtin.edit_spreadsheet import (
        set_selected_sheet,
        set_spreadsheet_snapshot,
    )

    set_spreadsheet_snapshot(file_id, body.snapshot)
    if body.selected_sheet:
        set_selected_sheet(body.selected_sheet)
    return JSONResponse(status_code=200, content={"ok": True})


@router.get("/{file_id}/spreadsheet-context")
async def get_spreadsheet_context(
    file_id: str,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Return the current workbook snapshot (may have been mutated by a chat edit)."""
    from pocketpaw.tools.builtin.edit_spreadsheet import get_spreadsheet_snapshot

    snapshot = get_spreadsheet_snapshot(file_id)
    if snapshot is None:
        return JSONResponse(
            status_code=404, content={"detail": "No spreadsheet editing session active."}
        )
    return JSONResponse(status_code=200, content={"snapshot": snapshot})


# --- slides ---------------------------------------------------------------


def _build_slides_system_prompt(
    deck: dict,
    selected_slide_id: str | None,
    user_prompt: str,
) -> str:
    """Build the system prompt for slides AI editing."""
    from pocketpaw.tools.builtin.edit_slides import build_slides_prompt_context

    ctx = build_slides_prompt_context(deck=deck, selected_slide_id=selected_slide_id)
    if ctx is None:
        ctx = (
            "The slide deck is currently empty (no slides). "
            "Use the edit_slides tool to create slides and populate them."
        )
    return (
        f"{ctx}\n\nThe user's request: {user_prompt}\n\n"
        "After making your edits, briefly tell the user what you changed."
    )


@router.post("/{file_id}/slides-edit")
async def slides_ai_edit(
    file_id: str,
    body: SlidesEditRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Run an AI agent to edit slides content. Returns the final deck."""
    deck: dict = body.content if isinstance(body.content, dict) else {}
    deck = json.loads(json.dumps(deck))

    system_prompt = _build_slides_system_prompt(
        deck=deck,
        selected_slide_id=body.selected_slide_id,
        user_prompt=body.prompt,
    )

    from pocketpaw.tools.builtin.edit_slides import (
        clear_edit_session,
        clear_selected_slide_id,
        set_edit_session,
        set_selected_slide_id,
        set_slides_data,
    )

    set_edit_session(deck)
    set_slides_data(file_id, deck)
    set_selected_slide_id(body.selected_slide_id)

    try:
        from pocketpaw.agents.pool import get_agent_pool

        pool = get_agent_pool()
        agent_id = await _resolve_default_agent(workspace_id)
        if not agent_id:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "slides_edit.no_agent",
                    "message": "No agent available in this workspace.",
                },
            )

        session_key = f"cloud:slides-edit:{file_id}:{agent_id}"
        summary_parts: list[str] = []

        async for event in pool.run(
            agent_id,
            body.prompt,
            session_key,
            knowledge_context=system_prompt,
        ):
            if event.type == "message":
                summary_parts.append(str(event.content))
            elif event.type == "error":
                return JSONResponse(
                    status_code=500,
                    content={"detail": "slides_edit.agent_error", "message": str(event.content)},
                )
            elif event.type == "done":
                break

        return JSONResponse(
            content=SlidesEditResponse(
                content=deck,
                summary="".join(summary_parts).strip() or "Slides edited.",
            ).model_dump(by_alias=True)
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"detail": "slides_edit.failed", "message": str(exc)},
        )
    finally:
        clear_edit_session()
        clear_selected_slide_id()


@router.put("/{file_id}/slides-context")
async def sync_slides_context(
    file_id: str,
    body: SyncSlidesContextRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Store the current slides deck for chat-initiated slides editing."""
    from pocketpaw.tools.builtin.edit_slides import (
        set_selected_slide_id,
        set_slides_data,
    )

    set_slides_data(file_id, body.content)
    if body.selected_slide_id:
        set_selected_slide_id(body.selected_slide_id)
    return JSONResponse(status_code=200, content={"ok": True})


@router.get("/{file_id}/slides-context")
async def get_slides_context(
    file_id: str,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Return the current slides deck (may have been mutated by a chat edit)."""
    from pocketpaw.tools.builtin.edit_slides import get_slides_data

    deck = get_slides_data(file_id)
    if deck is None:
        return JSONResponse(
            status_code=404, content={"detail": "No slides editing session active."}
        )
    return JSONResponse(status_code=200, content={"content": deck})
