"""Agents domain — FastAPI router.

Updated 2026-04-19 (feat/cluster-d-agent-scope-picker): added
``GET /agents/{id}/scope`` + ``PATCH /agents/{id}/scope`` for the
ScopePicker UI. PATCH re-validates and re-normalises the payload server
side — the frontend normaliseScope helper is treated as a UX nicety, not
a security boundary.

Updated 2026-05-07 (fix/rbac-guards-fabric-instinct-agent-knowledge): the
five knowledge mutation endpoints (POST text/url/urls/upload, DELETE) now
require ``require_agent_owner_or_admin`` — same guard as PATCH/DELETE agent
CRUD. Previously they had no RBAC guard beyond ``require_license``, so any
workspace member could inject content into any agent's knowledge base.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi import File as FastAPIFile
from starlette.responses import Response

from ee.cloud.agents import service as agents_service
from ee.cloud.agents.dto import (
    CreateAgentRequest,
    DiscoverRequest,
    ScopeAssignmentRequest,
    ScopeAssignmentResponse,
    UpdateAgentRequest,
    agent_to_dict,
)
from ee.cloud.license import require_license
from ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
    require_agent_owner_or_admin,
)

router = APIRouter(prefix="/agents", tags=["Agents"], dependencies=[Depends(require_license)])

# ---------------------------------------------------------------------------
# Backends discovery
# ---------------------------------------------------------------------------


@router.get("/backends")
async def list_available_backends():
    """List available agent backends with their display names."""
    from pocketpaw.agents.registry import get_backend_info, list_backends

    results = []
    for name in list_backends():
        try:
            info = get_backend_info(name)
            results.append(
                {
                    "name": name,
                    "displayName": info.display_name if info else name,
                    "available": info is not None,
                }
            )
        except Exception:
            results.append({"name": name, "displayName": name, "available": False})
    return results


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("", dependencies=[Depends(require_action_any_workspace("agent.create"))])
async def create_agent(
    body: CreateAgentRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    ctx = agents_service.legacy_ctx(user_id, workspace_id)
    return agent_to_dict(await agents_service.create(ctx, workspace_id, body))


@router.get("")
async def list_agents(
    workspace_id: str = Depends(current_workspace_id),
    query: str | None = Query(default=None),
) -> list[dict]:
    items = await agents_service.list_agents(workspace_id, query=query)
    return [agent_to_dict(a) for a in items]


@router.get("/{agent_id}")
async def get_agent(agent_id: str) -> dict:
    return agent_to_dict(await agents_service.get(agent_id))


@router.get("/uname/{slug}")
async def get_by_slug(
    slug: str,
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    return agent_to_dict(await agents_service.get_by_slug(workspace_id, slug))


@router.patch("/{agent_id}", dependencies=[Depends(require_agent_owner_or_admin)])
async def update_agent(
    agent_id: str,
    body: UpdateAgentRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    ctx = agents_service.legacy_ctx(user_id)
    return agent_to_dict(await agents_service.update(ctx, agent_id, body))


@router.delete("/{agent_id}", status_code=204, dependencies=[Depends(require_agent_owner_or_admin)])
async def delete_agent(
    agent_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    ctx = agents_service.legacy_ctx(user_id)
    await agents_service.delete(ctx, agent_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@router.post("/discover")
async def discover_agents(
    body: DiscoverRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> list[dict]:
    ctx = agents_service.legacy_ctx(user_id, workspace_id)
    items = await agents_service.discover(ctx, workspace_id, body)
    return [agent_to_dict(a) for a in items]


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------


@router.post(
    "/{agent_id}/knowledge/text",
    dependencies=[Depends(require_agent_owner_or_admin)],
)
async def ingest_text(agent_id: str, body: dict):
    """Ingest plain text into agent's knowledge base."""
    import logging

    from ee.cloud.agents.knowledge import KnowledgeService

    text = body.get("text", "")
    source = body.get("source", "manual")
    if not text:
        return {"error": "No text provided"}
    try:
        return await KnowledgeService.ingest_text(agent_id, text, source)
    except Exception as exc:
        logging.getLogger(__name__).error("Knowledge ingest failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


@router.post(
    "/{agent_id}/knowledge/url",
    dependencies=[Depends(require_agent_owner_or_admin)],
)
async def ingest_url(agent_id: str, body: dict):
    """Fetch and ingest a URL into agent's knowledge base."""
    from ee.cloud.agents.knowledge import KnowledgeService

    url = body.get("url", "")
    if not url:
        return {"error": "No URL provided"}
    return await KnowledgeService.ingest_url(agent_id, url)


@router.post(
    "/{agent_id}/knowledge/urls",
    dependencies=[Depends(require_agent_owner_or_admin)],
)
async def ingest_urls(agent_id: str, body: dict):
    """Batch ingest multiple URLs."""
    from ee.cloud.agents.knowledge import KnowledgeService

    urls = body.get("urls", [])
    results = []
    for url in urls:
        result = await KnowledgeService.ingest_url(agent_id, url)
        results.append(result)
    return results


@router.get("/{agent_id}/knowledge/search")
async def search_knowledge(agent_id: str, q: str = Query(..., min_length=1), limit: int = 5):
    """Search agent's knowledge base."""
    from ee.cloud.agents.knowledge import KnowledgeService

    results = await KnowledgeService.search(agent_id, q, limit)
    return {"results": results}


@router.get("/{agent_id}/knowledge")
async def list_knowledge(agent_id: str):
    """List all knowledge articles for an agent."""
    from ee.cloud.agents.knowledge import KnowledgeService

    try:
        articles = await KnowledgeService.list_articles(agent_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to list knowledge: {exc}")
    return {"items": articles}


@router.get("/{agent_id}/knowledge/{article_id}")
async def get_knowledge_article(agent_id: str, article_id: str):
    """Fetch the full body of a single knowledge article."""
    from ee.cloud.agents.knowledge import KnowledgeService

    try:
        return await KnowledgeService.get_article(agent_id, article_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=f"Article not found: {exc}")


# ---------------------------------------------------------------------------
# Profile Picture Upload
# ---------------------------------------------------------------------------


@router.post("/{agent_id}/profile-pic")
async def upload_profile_pic(
    agent_id: str,
    request: Request,
    file: UploadFile = FastAPIFile(...),
    user_id: str = Depends(current_user_id),
):
    """Upload a profile picture for an agent."""
    import uuid
    from pathlib import Path

    from fastapi import HTTPException

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Validate file type
    allowed = {"image/jpeg", "image/png", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, and WebP images are allowed")

    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File size must be under 5 MB")

    # Save to ~/.pocketpaw/uploads/avatars/
    ext = Path(file.filename).suffix.lower() or ".png"
    upload_dir = Path.home() / ".pocketpaw" / "uploads" / "avatars"
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{agent_id}-{uuid.uuid4().hex[:8]}{ext}"
    dest = upload_dir / filename
    dest.write_bytes(content)

    # Build full URL using the request's base URL
    base = str(request.base_url).rstrip("/")
    avatar_url = f"{base}/uploads/avatars/{filename}"

    # Update the agent's avatar field
    ctx = agents_service.legacy_ctx(user_id)
    await agents_service.update(ctx, agent_id, UpdateAgentRequest(avatar=avatar_url))

    return {"url": avatar_url}


@router.post(
    "/{agent_id}/knowledge/upload",
    dependencies=[Depends(require_agent_owner_or_admin)],
)
async def upload_and_ingest(
    agent_id: str,
    file: UploadFile = FastAPIFile(...),  # noqa: B008
):
    """Upload a file and ingest into agent's knowledge base.

    Supports: .pdf, .txt, .md, .csv, .json, .docx, .png, .jpg, .jpeg, .webp
    """
    import tempfile
    from pathlib import Path

    from ee.cloud.agents.knowledge import KnowledgeService

    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    suffix = Path(file.filename).suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        try:
            result = await KnowledgeService.ingest_file(agent_id, tmp_path, source=file.filename)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=f"Knowledge ingestion failed: {exc}")
        if not isinstance(result, dict):
            result = {"result": result}
        result["originalName"] = file.filename
        result["size"] = len(content)
        return result
    finally:
        import os

        os.unlink(tmp_path)


@router.delete(
    "/{agent_id}/knowledge",
    status_code=204,
    dependencies=[Depends(require_agent_owner_or_admin)],
)
async def clear_knowledge(agent_id: str):
    """Clear all knowledge for an agent."""
    from ee.cloud.agents.knowledge import KnowledgeService

    await KnowledgeService.clear(agent_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Scope assignment — read + write the scope tag list on an agent.
# Both endpoints require the caller to be the agent owner or a workspace
# admin (``require_agent_owner_or_admin``). PATCH additionally re-runs the
# scope validator server-side via ``ScopeAssignmentRequest`` so a direct
# API call cannot bypass the frontend ScopePicker's normaliseScope guard.
# ---------------------------------------------------------------------------


@router.get(
    "/{agent_id}/scope",
    response_model=ScopeAssignmentResponse,
    dependencies=[Depends(require_agent_owner_or_admin)],
)
async def get_agent_scope(agent_id: str) -> ScopeAssignmentResponse:
    """Return the scope tags currently assigned to an agent.

    Empty list means "no scope narrowing" — the agent sees everything in
    its workspace. Non-empty list bounds retrieval + fabric queries.
    """
    scopes = await agents_service.get_scopes(agent_id)
    return ScopeAssignmentResponse(agent_id=agent_id, scopes=scopes)


@router.patch(
    "/{agent_id}/scope",
    response_model=ScopeAssignmentResponse,
    dependencies=[Depends(require_agent_owner_or_admin)],
)
async def set_agent_scope(
    agent_id: str,
    body: ScopeAssignmentRequest,
) -> ScopeAssignmentResponse:
    """Replace the agent's scope assignment (full-list swap).

    The request body is validated + normalised by
    ``ScopeAssignmentRequest``; the service re-applies the validator so a
    fleet installer calling ``set_scopes`` directly gets the same
    guarantees.
    """
    updated = await agents_service.set_scopes(agent_id, body.scopes)
    return ScopeAssignmentResponse(agent_id=agent_id, scopes=updated)
