# ee/pocketpaw_ee/cloud/studio/router.py — the direct describe-to-media surface
# (paw-enterprise /studio composer + gallery).
#
# Every endpoint is the thin adapter over ``studio.service`` per the EE
# "primitive = service + thin adapters" shape: no business logic here, only
# HTTP→service→HTTP mapping. License-gated exactly like the catalog router.
#
#   GET  /studio/models          → StudioModelsResponse   (gateway-backed)
#   GET  /studio/styles          → StudioStylesResponse   (static)
#   GET  /studio/generations     → GenerationsResponse    (per-workspace history)
#   POST /studio/generate        → Generation             (LiteLLM + fal.ai)
#   GET  /studio/generations/{id}→ Generation
#   POST /studio/edit            → Generation             (fal.ai edit endpoints)
#   POST /studio/video-elements  → Generation             (fal.ai Kling Elements)
#   POST /studio/video-motion-control → Generation        (fal.ai Kling Motion Control)
#   POST /studio/suggest-prompt  → PromptSuggestion       (heuristic, no LLM)
#
# The tenant is attached per-request via ``current_workspace_id`` (the frontend
# already sends X-Workspace-Id on every call). It scopes the history reads AND is
# tagged as the OpenAI ``user`` on each proxy generation so proxy spend attributes
# to the workspace. Mounted in ``mount_cloud()``.
#
# Created 2026-08-17 (studio-real-backend): new entity router.

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from pocketpaw_ee.catalog.litellm_client import CatalogUpstreamError
from pocketpaw_ee.cloud._core.deps import current_workspace_id
from pocketpaw_ee.cloud.license import require_license

from . import schemas, service

router = APIRouter(prefix="/studio", tags=["Studio"], dependencies=[Depends(require_license)])


@router.get("/models", response_model=schemas.StudioModelsResponse)
async def list_models() -> schemas.StudioModelsResponse:
    """List the image/video models the LiteLLM gateway serves, shaped for the
    /studio picker. A proxy outage returns 502 (the source of truth is
    unreachable) — same convention as the catalog router."""
    try:
        models = await service.list_models()
    except CatalogUpstreamError as exc:
        raise HTTPException(502, f"Model catalog source unavailable: {exc}") from exc
    return schemas.StudioModelsResponse(models=models)


@router.get("/styles", response_model=schemas.StudioStylesResponse)
async def list_styles() -> schemas.StudioStylesResponse:
    """List the one-tap style catalog (same set the frontend mock shipped)."""
    return schemas.StudioStylesResponse(styles=service.list_styles())


@router.get("/generations", response_model=schemas.GenerationsResponse)
async def list_generations(
    workspace_id: str = Depends(current_workspace_id),
) -> schemas.GenerationsResponse:
    """Return the workspace's generation history, newest first (persisted, so it
    survives reloads)."""
    return schemas.GenerationsResponse(generations=service.list_generations(workspace_id))


@router.post("/generate", response_model=schemas.Generation)
async def generate(
    req: schemas.GenerateRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> schemas.Generation:
    """Run a direct studio generation through the LiteLLM gateway (fal.ai image
    models). Image requests resolve synchronously to ``succeeded``; video
    requests return 501 until the gateway serves video models."""
    try:
        return await service.generate(req, workspace_id=workspace_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except service.StudioNotSupported as exc:
        raise HTTPException(501, str(exc)) from exc
    except service.StudioUpstreamError as exc:
        raise HTTPException(502, f"Image generation failed: {exc}") from exc
    except CatalogUpstreamError as exc:
        raise HTTPException(502, f"Model catalog source unavailable: {exc}") from exc


@router.get("/generations/{gen_id}", response_model=schemas.Generation)
async def get_generation(
    gen_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> schemas.Generation:
    """Return one generation by id (scoped to the workspace), or 404."""
    generation = service.get_generation(gen_id, workspace_id)
    if generation is None:
        raise HTTPException(404, f"Generation '{gen_id}' not found")
    return generation


@router.post("/edit", response_model=schemas.Generation)
async def edit(
    req: schemas.EditRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> schemas.Generation:
    """Run a canvas edit op (inpaint/expand/upscale/variations/remove-bg/edit/
    sketch-to-image) directly against fal.ai.

    The LiteLLM gateway serves generation models only — fal's image-edit
    endpoints are called by the service (cloud.studio.fal_edit), and the result
    is saved through media storage as a NEW generation. Bad input (unknown op /
    missing prompt / bad sourceUrl) returns 400; a fal upstream failure 502."""
    try:
        return await service.edit(req, workspace_id=workspace_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except service.StudioNotSupported as exc:
        raise HTTPException(501, str(exc)) from exc
    except service.StudioUpstreamError as exc:
        raise HTTPException(502, f"Image edit failed: {exc}") from exc


@router.post("/video-elements", response_model=schemas.Generation)
async def video_elements(
    req: schemas.VideoElementsRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> schemas.Generation:
    """Run the "Edit video" panel's Kling Elements call directly against fal.ai.

    Accepts a source video (≤30s), up to 20 element/reference images, and a
    prompt; the result video is persisted as a NEW generation. Bad input (too
    many images / source over 30s / missing everything) returns 400; a fal
    upstream failure 502."""
    try:
        return await service.generate_video_elements(req, workspace_id=workspace_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except service.StudioUpstreamError as exc:
        raise HTTPException(502, f"Video edit failed: {exc}") from exc


@router.post("/video-motion-control", response_model=schemas.Generation)
async def video_motion_control(
    req: schemas.VideoMotionRequest,
    workspace_id: str = Depends(current_workspace_id),
) -> schemas.Generation:
    """Run the "Motion control" panel's Kling Motion Control call directly
    against fal.ai.

    Accepts a character image (visible face and body) and a reference motion
    video; the result video is persisted as a NEW generation. Bad input (missing
    character image / motion video) returns 400; a fal upstream failure 502."""
    try:
        return await service.generate_video_motion(req, workspace_id=workspace_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except service.StudioUpstreamError as exc:
        raise HTTPException(502, f"Motion control failed: {exc}") from exc


@router.post("/suggest-prompt", response_model=schemas.PromptSuggestion)
async def suggest_prompt(req: schemas.SuggestPromptRequest) -> schemas.PromptSuggestion:
    """Enrich a plain sentence into a generation prompt + inferred media kind.
    Heuristic mirror of the mock (no LLM call)."""
    return service.suggest_prompt(req.sentence)


# ── Flow projects (persisted server-side, workspace-scoped) ─────────────────
# The /studio flow editor's canvases. The frontend keeps a debounced save of the
# active canvas and calls PUT (an UPSERT — create-or-update), so an offline-first
# cache plus a server round-trip keeps every project across devices. Node/edge
# payloads are opaque (the backend never inspects them).


@router.get("/flow-projects", response_model=schemas.FlowProjectsResponse)
async def list_flow_projects(
    workspace_id: str = Depends(current_workspace_id),
) -> schemas.FlowProjectsResponse:
    """Return the workspace's flow projects, most-recently-updated first."""
    return schemas.FlowProjectsResponse(
        projects=service.list_flow_projects(workspace_id),
    )


@router.put("/flow-projects/{project_id}", response_model=schemas.FlowProject)
async def save_flow_project(
    project_id: str,
    req: schemas.FlowProjectSave,
    workspace_id: str = Depends(current_workspace_id),
) -> schemas.FlowProject:
    """Create-or-update a flow project (UPSERT). ``name`` is preserved when
    omitted, so a canvas-only save never wipes the title."""
    return service.save_flow_project(
        project_id,
        workspace_id,
        name=req.name,
        nodes=req.nodes,
        edges=req.edges,
    )


@router.delete("/flow-projects/{project_id}", status_code=204)
async def delete_flow_project(
    project_id: str,
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    """Delete one flow project, or 404 if it doesn't exist in this workspace."""
    if not service.delete_flow_project(project_id, workspace_id):
        raise HTTPException(404, f"Flow project '{project_id}' not found")
