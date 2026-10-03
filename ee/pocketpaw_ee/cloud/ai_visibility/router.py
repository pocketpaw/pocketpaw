# AI visibility — HTTP routes. Both routers mount under /api/v1 from ``cloud/__init__.py``.
#
# ``router`` (public):
#   POST /tools/ai-check — free check: does ChatGPT name this business? Per-IP
#                          5/hour, Turnstile, daily spend cap (``public_check.py``).
#                          No user dependency: the dashboard auth middleware lets
#                          /api/v1/* through, so no exemption entry is needed.
#
# ``site_router`` (signed in, gated like the sites router: the "sites" plan
# feature, fabric.read to read, fabric.write to change; tenant-scoped, so a missing
# or cross-tenant site is 404):
#   GET  /sites/{id}/ai-visibility              — the Staff card
#   PUT  /sites/{id}/ai-visibility/questions    — save 1-10 questions
#   POST /sites/{id}/ai-visibility/check        — 202; Staff only, once per 24h
#   POST /sites/{id}/ai-visibility/apply-fix    — 202; republish for our own fixes
# ``PATCH /sites/{id}/ai-visibility`` (the training opt-in) lives in the sites router.
#
# Thin: no Beanie doc import here (import-linter "AiVisibility" contract); errors
# are CloudError subclasses mapped by the global handler.

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace, require_plan_feature
from pocketpaw_ee.cloud._core.rate_limit import client_ip, rate_limit_ai_check_public
from pocketpaw_ee.cloud.ai_visibility import service
from pocketpaw_ee.cloud.ai_visibility.dto import (
    AiCheckRequest,
    AiCheckResponse,
    ApplyFixRequest,
    ApplyFixResponse,
    CheckQueuedResponse,
    SetQuestionsRequest,
    SiteVisibilityResponse,
)
from pocketpaw_ee.cloud.ai_visibility.public_check import run_public_check
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(tags=["ai-visibility"], dependencies=[Depends(require_license)])

site_router = APIRouter(
    tags=["ai-visibility"],
    dependencies=[Depends(require_license), Depends(require_plan_feature("sites"))],
)


@router.post(
    "/tools/ai-check",
    response_model=AiCheckResponse,
    dependencies=[Depends(rate_limit_ai_check_public)],
)
async def free_ai_check(body: AiCheckRequest, request: Request) -> AiCheckResponse:
    """PUBLIC — no sign-in. Ask ChatGPT (3 questions x 2 runs) whether it names the
    business. 400 ``tools.ai_check.turnstile_failed``, 429 ``tools.ai_check.rate_limited``,
    503 ``tools.ai_check.daily_limit``, 502 ``tools.ai_check.engine_failed``."""
    return await run_public_check(body, client_ip(request))


@site_router.get("/sites/{site_id}/ai-visibility", response_model=SiteVisibilityResponse)
async def get_site_visibility(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.read")),
) -> dict:
    """The site's AI visibility card: training opt-in, whether its plan includes
    checks, the saved questions, and the latest check (null before the first)."""
    return await service.get_site_visibility(ctx.workspace_id, site_id)


@site_router.put("/sites/{site_id}/ai-visibility/questions", response_model=SiteVisibilityResponse)
async def set_site_questions(
    site_id: str,
    body: SetQuestionsRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> dict:
    """Save the 1-10 questions the next check asks. Returns the card."""
    return await service.set_questions(ctx.workspace_id, site_id, body)


@site_router.post(
    "/sites/{site_id}/ai-visibility/check",
    response_model=CheckQueuedResponse,
    status_code=202,
)
async def request_site_check(
    site_id: str,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> dict:
    """Queue a check now. 403 ``ai_visibility.plan_required`` off Staff, 429
    ``ai_visibility.too_soon`` within 24h of the last one, 422 with no questions."""
    return await service.request_check(ctx.workspace_id, site_id)


@site_router.post(
    "/sites/{site_id}/ai-visibility/apply-fix",
    response_model=ApplyFixResponse,
    status_code=202,
)
async def apply_site_fix(
    site_id: str,
    body: ApplyFixRequest,
    ctx: RequestContext = Depends(request_context),
    _: object = Depends(require_action_any_workspace("fabric.write")),
) -> dict:
    """Start the site's republish for the fix its latest check picked, when Paw
    Sites applies it itself (``ai_access``). 403 ``ai_visibility.plan_required``
    off Staff; 400 ``ai_visibility.fix_not_applicable`` for any other fix id."""
    return await service.apply_fix(ctx.workspace_id, ctx.user_id, site_id, body)
