"""FileVersions router — inline-edit API for the Files panel.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ee.cloud._core.errors import CloudError, ConflictError, NotFound
from ee.cloud.file_versions import service as _svc
from ee.cloud.file_versions.dto import (
    AiEditRequest,
    AiEditResponse,
    UpdateFileContentRequest,
    WriteFileRequest,
)
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id

router = APIRouter(
    prefix="/files",
    tags=["File Versions"],
    dependencies=[Depends(require_license)],
)


def _ctx(user_id: str, workspace_id: str):
    """Build a minimal RequestContext from dependency results."""
    from datetime import UTC, datetime

    from ee.cloud._core.context import RequestContext, ScopeKind
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# POST /files/write — Create or overwrite a file
# ---------------------------------------------------------------------------


@router.post("/write")
async def write_file(
    body: WriteFileRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Create or overwrite a file. Returns the file id for subsequent versioned saves."""
    ctx = _ctx(user_id, workspace_id)
    try:
        result = await _svc.write_file(ctx, body)
    except CloudError as e:
        return JSONResponse(status_code=e.status_code, content=e.to_dict())
    return JSONResponse(status_code=201, content=result.model_dump(by_alias=True, mode="json"))


# ---------------------------------------------------------------------------
# PUT /files/{id} — Inline edit
# ---------------------------------------------------------------------------


@router.put("/{file_id}")
async def update_file(
    file_id: str,
    body: UpdateFileContentRequest,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
    if_match: str | None = Header(None, alias="If-Match"),
) -> JSONResponse:
    """Replace a text file's content inline.

    Request body: ``{ content: "<new text>", expectedVersion?: <int> }``
    Header: ``If-Match: <current content_version>`` (for optimistic concurrency)

    Returns the new version info on success.
    Returns 412 if the version precondition fails.
    Returns 422 if the file type isn't editable.
    """
    ctx = _ctx(user_id, workspace_id)

    # If-Match header takes precedence over body field
    if if_match is not None:
        try:
            body.expected_version = int(if_match.strip('"'))
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"detail": "files.bad_version", "message": "If-Match must be an integer."},
            )

    try:
        result = await _svc.update_file_content(ctx, file_id, body)
    except NotFound as e:
        return JSONResponse(status_code=404, content=e.to_dict())
    except ConflictError as e:
        return JSONResponse(status_code=412, content=e.to_dict())
    except CloudError as e:
        return JSONResponse(status_code=e.status_code, content=e.to_dict())

    return JSONResponse(status_code=200, content=result.model_dump(by_alias=True, mode="json"))


# ---------------------------------------------------------------------------
# GET /files/{id}/versions — Version list
# ---------------------------------------------------------------------------


@router.get("/{file_id}/versions")
async def list_file_versions(
    file_id: str,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """List all archived versions for a file (oldest first, no content)."""
    ctx = _ctx(user_id, workspace_id)
    versions = await _svc.list_versions(ctx, file_id)
    return JSONResponse(content={
        "versions": [v.model_dump(by_alias=True, mode="json") for v in versions],
    })


# ---------------------------------------------------------------------------
# GET /files/{id}/versions/{version_id} — Single version
# ---------------------------------------------------------------------------


@router.get("/{file_id}/versions/{version_id}")
async def get_file_version(
    file_id: str,
    version_id: str,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Fetch a single version with full content (for revert preview / diff)."""
    ctx = _ctx(user_id, workspace_id)
    try:
        version = await _svc.get_version(ctx, file_id, version_id)
    except NotFound as e:
        return JSONResponse(status_code=404, content=e.to_dict())
    return JSONResponse(content=version.model_dump(by_alias=True, mode="json"))


# ---------------------------------------------------------------------------
# POST /files/{id}/versions/{version_id}/revert — Revert
# ---------------------------------------------------------------------------


@router.post("/{file_id}/versions/{version_id}/revert")
async def revert_file_version(
    file_id: str,
    version_id: str,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Revert the live file to a historical version."""
    ctx = _ctx(user_id, workspace_id)
    try:
        result = await _svc.revert_to_version(ctx, file_id, version_id)
    except NotFound as e:
        return JSONResponse(status_code=404, content=e.to_dict())
    except ConflictError as e:
        return JSONResponse(status_code=412, content=e.to_dict())
    except CloudError as e:
        return JSONResponse(status_code=e.status_code, content=e.to_dict())
    return JSONResponse(status_code=200, content=result.model_dump(by_alias=True, mode="json"))


# ---------------------------------------------------------------------------
# GET /files/{id}/versions/{v1}/diff/{v2} — Diff
# ---------------------------------------------------------------------------


@router.get("/{file_id}/versions/{v1}/diff/{v2}")
async def diff_file_versions(
    file_id: str,
    v1: str,
    v2: str,
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
) -> JSONResponse:
    """Return a unified diff between two historical versions."""
    ctx = _ctx(user_id, workspace_id)
    try:
        diff = await _svc.diff_versions(ctx, file_id, v1, v2)
    except NotFound as e:
        return JSONResponse(status_code=404, content=e.to_dict())
    return JSONResponse(content=diff.model_dump(by_alias=True, mode="json"))


# ---------------------------------------------------------------------------
# POST /files/{id}/ai-edit — AI-assisted editing
# ---------------------------------------------------------------------------


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
    ``edit_document`` MCP tool can access them, and runs the workspace's
    default agent. The agent can call the tool multiple times to iterate on
    edits. Returns the final blocks array to the frontend.
    """
    body = AiEditRequest.model_validate(body)

    import json as _json

    # Parse Editor.js JSON → blocks with id, type, data
    try:
        doc = _json.loads(body.content)
        blocks: list[dict] = doc.get("blocks", []) if isinstance(doc, dict) else []
    except (_json.JSONDecodeError, TypeError):
        blocks = []

    # Deep-copy so the ContextVar stores a mutable list independent of the
    # parsed JSON. Each block dict is also copied so updates don't corrupt
    # the original parse result.
    blocks = [_json.loads(_json.dumps(b)) for b in blocks]

    # Build the system prompt dynamically from the actual blocks
    system_prompt = _build_edit_system_prompt(
        blocks=blocks,
        available_tools=body.available_tools,
        selected_block_id=body.selected_block_id,
        user_prompt=body.prompt,
    )
    # Store blocks in ContextVar AND the module-level dict so the MCP
    # handler can access them regardless of how the SDK dispatches the tool.
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

        from ee.cloud.agents import service as agents_service

        agents = await agents_service.list_agents(workspace_id)
        agent_id = agents[0].id if agents else None

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
                    content={
                        "detail": "ai_edit.agent_error",
                        "message": str(event.content),
                    },
                )
            elif event.type == "done":
                break

        # After the agent finishes, read the final blocks (may have been
        # mutated by edit_document tool calls via MCP).
        final_blocks = blocks

        return JSONResponse(
            content=AiEditResponse(
                blocks=final_blocks,
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


# ---------------------------------------------------------------------------
# PUT /files/{id}/editing-context — Sync editor blocks before chat
# ---------------------------------------------------------------------------


class SyncEditingContextRequest(BaseModel):
    blocks: list[dict]


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


# ---------------------------------------------------------------------------
# GET /files/{id}/editing-context — Fetch updated blocks after chat
# ---------------------------------------------------------------------------


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

