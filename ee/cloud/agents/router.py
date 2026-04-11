"""Agents domain — FastAPI router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, UploadFile
from fastapi import File as FastAPIFile
from starlette.responses import Response

from ee.cloud.agents.schemas import (
    CreateAgentRequest,
    DiscoverRequest,
    UpdateAgentRequest,
)
from ee.cloud.agents.service import AgentService
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id

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


@router.post("")
async def create_agent(
    body: CreateAgentRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    return await AgentService.create(workspace_id, user_id, body)


@router.get("")
async def list_agents(
    workspace_id: str = Depends(current_workspace_id),
    query: str | None = Query(default=None),
) -> list[dict]:
    return await AgentService.list_agents(workspace_id, query)


@router.get("/{agent_id}")
async def get_agent(agent_id: str) -> dict:
    return await AgentService.get(agent_id)


@router.get("/uname/{slug}")
async def get_by_slug(
    slug: str,
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    return await AgentService.get_by_slug(workspace_id, slug)


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str,
    body: UpdateAgentRequest,
    user_id: str = Depends(current_user_id),
) -> dict:
    return await AgentService.update(agent_id, user_id, body)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    user_id: str = Depends(current_user_id),
) -> Response:
    await AgentService.delete(agent_id, user_id)
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
    return await AgentService.discover(workspace_id, user_id, body)


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------


@router.post("/{agent_id}/knowledge/text")
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


@router.post("/{agent_id}/knowledge/url")
async def ingest_url(agent_id: str, body: dict):
    """Fetch and ingest a URL into agent's knowledge base."""
    from ee.cloud.agents.knowledge import KnowledgeService

    url = body.get("url", "")
    if not url:
        return {"error": "No URL provided"}
    return await KnowledgeService.ingest_url(agent_id, url)


@router.post("/{agent_id}/knowledge/urls")
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


@router.post("/{agent_id}/knowledge/upload")
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
        return {"error": "No filename provided"}

    suffix = Path(file.filename).suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = await KnowledgeService.ingest_file(agent_id, tmp_path)
        result["originalName"] = file.filename
        result["size"] = len(content)
        return result
    finally:
        import os

        os.unlink(tmp_path)


@router.delete("/{agent_id}/knowledge", status_code=204)
async def clear_knowledge(agent_id: str):
    """Clear all knowledge for an agent."""
    from ee.cloud.agents.knowledge import KnowledgeService

    await KnowledgeService.clear(agent_id)
    return Response(status_code=204)
