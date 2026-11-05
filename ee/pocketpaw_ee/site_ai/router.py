# ee/pocketpaw_ee/site_ai/router.py — the site AI HTTP surface.
#
# ``router`` (owner routes: session + workspace RBAC + the ``sites`` plan feature).
# Reads need ``fabric.read``; every mutation needs ``sites.ai_manage`` (ADMIN) and
# writes an audit row (service.py). The pocket must be in the caller's workspace.
#   GET/PUT     /sites/by-pocket/{pocket_id}/ai/settings
#   GET         /sites/by-pocket/{pocket_id}/ai/models    agents + catalog models an
#               alias can be mapped to (``alias_models`` on settings)
#   GET/POST    /sites/by-pocket/{pocket_id}/ai/presets
#   GET/PUT/DELETE /sites/by-pocket/{pocket_id}/ai/presets/{preset_id}
#   GET/PUT     /sites/by-pocket/{pocket_id}/ai/budget
#   GET         /sites/by-pocket/{pocket_id}/ai/usage?from&to
#   GET         /sites/by-pocket/{pocket_id}/ai/transcripts?limit&before  (sites.ai_manage:
#               opt-in transcripts are visitors' words, so reading them is ADMIN too)
#
# ``public_router`` (no session by design; the site gates in chat.py are the auth):
#   POST /sites/{site_id}/ai/ticket  site key + origin gate + Turnstile -> visitor ticket
#   POST /sites/{site_id}/ai/chat    PAW_GATEWAY_TOKEN (the site's Worker), or the site
#                                    key + origin gate + ticket (browser / own preview)
#   POST /sites/{site_id}/ai/embed   the same callers as chat; always 503 (embeddings
#                                    aren't offered)
#
# Wire shapes are pinned by the Sites AI apps contract; see dto.py. Mounted in
# ``cloud.mount_cloud``; listed in tests/cloud/auth/test_route_auth_audit.py.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.site_ai import chat, embed, service
from pocketpaw_ee.site_ai.dto import (
    SiteAiBudget,
    SiteAiBudgetPut,
    SiteAiModels,
    SiteAiPreset,
    SiteAiPresetBody,
    SiteAiSettings,
    SiteAiSettingsPut,
    SiteAiTranscript,
    SiteAiUsage,
)

router = APIRouter(tags=["Site AI"], dependencies=[Depends(require_plan_feature("sites"))])
public_router = APIRouter(tags=["Site AI"])

_SITE = "/sites/by-pocket/{pocket_id}/ai"
_READ = Depends(require_action_any_workspace("fabric.read"))
_MANAGE = Depends(require_action_any_workspace("sites.ai_manage"))


@router.get(_SITE + "/settings", response_model=SiteAiSettings)
async def get_settings(
    pocket_id: str, ctx: RequestContext = Depends(request_context), _: object = _READ
) -> SiteAiSettings:
    return await service.get_settings(ctx.workspace_id or "", pocket_id)


@router.put(_SITE + "/settings", response_model=SiteAiSettings)
async def put_settings(
    pocket_id: str,
    body: SiteAiSettingsPut,
    ctx: RequestContext = Depends(request_context),
    _: object = _MANAGE,
) -> SiteAiSettings:
    return await service.put_settings(ctx.workspace_id or "", ctx.user_id, pocket_id, body)


@router.get(_SITE + "/models", response_model=SiteAiModels)
async def list_models(
    pocket_id: str, ctx: RequestContext = Depends(request_context), _: object = _READ
) -> SiteAiModels:
    return await service.list_models(ctx.workspace_id or "", ctx.user_id, pocket_id)


@router.get(_SITE + "/presets", response_model=list[SiteAiPreset])
async def list_presets(
    pocket_id: str, ctx: RequestContext = Depends(request_context), _: object = _READ
) -> list[SiteAiPreset]:
    return await service.list_presets(ctx.workspace_id or "", pocket_id)


@router.post(_SITE + "/presets", response_model=SiteAiPreset, status_code=201)
async def create_preset(
    pocket_id: str,
    body: SiteAiPresetBody,
    ctx: RequestContext = Depends(request_context),
    _: object = _MANAGE,
) -> SiteAiPreset:
    return await service.create_preset(ctx.workspace_id or "", ctx.user_id, pocket_id, body)


@router.get(_SITE + "/presets/{preset_id}", response_model=SiteAiPreset)
async def get_preset(
    pocket_id: str,
    preset_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = _READ,
) -> SiteAiPreset:
    return await service.get_preset(ctx.workspace_id or "", pocket_id, preset_id)


@router.put(_SITE + "/presets/{preset_id}", response_model=SiteAiPreset)
async def update_preset(
    pocket_id: str,
    preset_id: str,
    body: SiteAiPresetBody,
    ctx: RequestContext = Depends(request_context),
    _: object = _MANAGE,
) -> SiteAiPreset:
    return await service.update_preset(
        ctx.workspace_id or "", ctx.user_id, pocket_id, preset_id, body
    )


@router.delete(_SITE + "/presets/{preset_id}", status_code=204)
async def delete_preset(
    pocket_id: str,
    preset_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = _MANAGE,
) -> Response:
    await service.delete_preset(ctx.workspace_id or "", ctx.user_id, pocket_id, preset_id)
    return Response(status_code=204)


@router.get(_SITE + "/budget", response_model=SiteAiBudget)
async def get_budget(
    pocket_id: str, ctx: RequestContext = Depends(request_context), _: object = _READ
) -> SiteAiBudget:
    return await service.get_budget(ctx.workspace_id or "", pocket_id)


@router.put(_SITE + "/budget", response_model=SiteAiBudget)
async def put_budget(
    pocket_id: str,
    body: SiteAiBudgetPut,
    ctx: RequestContext = Depends(request_context),
    _: object = _MANAGE,
) -> SiteAiBudget:
    return await service.put_budget(ctx.workspace_id or "", ctx.user_id, pocket_id, body)


@router.get(_SITE + "/usage", response_model=SiteAiUsage)
async def get_usage(
    pocket_id: str,
    from_: str | None = Query(None, alias="from", max_length=40),
    to: str | None = Query(None, max_length=40),
    ctx: RequestContext = Depends(request_context),
    _: object = _READ,
) -> SiteAiUsage:
    return await service.usage(ctx.workspace_id or "", pocket_id, from_, to)


@router.get(_SITE + "/transcripts", response_model=list[SiteAiTranscript])
async def list_transcripts(
    pocket_id: str,
    limit: int = Query(20, ge=1, le=100),
    before: str | None = Query(None, max_length=40),
    ctx: RequestContext = Depends(request_context),
    _: object = _MANAGE,
) -> list[SiteAiTranscript]:
    return await service.list_transcripts(ctx.workspace_id or "", pocket_id, limit, before)


@public_router.post("/sites/{site_id}/ai/chat")
async def ai_chat(site_id: str, request: Request) -> Response:
    """OpenAI-shaped chat for a site. See chat.py for the gate order."""
    return await chat.handle(request, site_id)


@public_router.post("/sites/{site_id}/ai/ticket")
async def ai_ticket(site_id: str, request: Request) -> Response:
    """A ~20 minute visitor ticket for browser AI calls. See tickets.py."""
    return await chat.handle_ticket(request, site_id)


@public_router.post("/sites/{site_id}/ai/embed")
async def ai_embed(site_id: str, request: Request) -> Response:
    """Embeddings for a site (``input`` up to 64 strings). See embed.py."""
    return await embed.handle(request, site_id)
