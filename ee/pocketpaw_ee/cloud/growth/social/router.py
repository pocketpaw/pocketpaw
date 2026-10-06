# ee/pocketpaw_ee/cloud/growth/social/router.py — FastAPI router for Growth ›
# Social, mounted under ``/api/v1`` beside the growth router (routes live at
# ``/api/v1/growth/social/...``). A thin shell over ``growth.social.service``.
# Every route carries the license gate, ``request_context`` and a per-route
# RBAC guard (pinned by the guard-coverage test in tests/cloud/growth/
# test_gate.py): the two GETs ``growth.read``, everything else
# ``growth.write``. Nothing here reaches outside the workspace — no posting, no
# account connections — so there is no ``growth.manage`` verb.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.domain import IdeaStatus, Platform
from pocketpaw_ee.cloud.growth.social.dto import (
    GenerateIdeasRequest,
    ScheduleIdeasRequest,
    SocialIdeaListResponse,
    SocialIdeaResponse,
    SocialProfileListResponse,
    SocialProfileResponse,
    UpdateIdeaRequest,
    UpsertProfileRequest,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/growth/social", tags=["Growth"], dependencies=[Depends(require_license)]
)


_PROFILE_ID = Query(default=None, description="Which profile; omitted = the most recently updated")


@router.get(
    "/profiles",
    response_model=SocialProfileListResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def list_profiles(
    ctx: RequestContext = Depends(request_context),
) -> SocialProfileListResponse:
    """Every social profile in the workspace, oldest first."""
    return await social_service.list_profiles(ctx)


@router.post(
    "/profiles",
    response_model=SocialProfileResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def create_profile(ctx: RequestContext = Depends(request_context)) -> SocialProfileResponse:
    """Start a new, empty profile for another brand."""
    return await social_service.create_profile(ctx)


@router.get(
    "/profile",
    response_model=SocialProfileResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def get_profile(
    profile_id: str | None = _PROFILE_ID, ctx: RequestContext = Depends(request_context)
) -> SocialProfileResponse:
    """One social profile. 404 until the first PUT creates it."""
    return await social_service.get_profile(ctx, profile_id)


@router.put(
    "/profile",
    response_model=SocialProfileResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def upsert_profile(
    body: UpsertProfileRequest,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> SocialProfileResponse:
    """Partial upsert of the typed fields: omitted fields are left alone, an
    explicit ``null`` clears, ``description`` merges key by key."""
    return await social_service.upsert_profile(ctx, body, profile_id)


@router.post(
    "/profile/analyze",
    response_model=SocialProfileResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def analyze_profile(
    profile_id: str | None = _PROFILE_ID, ctx: RequestContext = Depends(request_context)
) -> SocialProfileResponse:
    """Read the website and run the analyst in-request (20–60 s). 200 even when
    ``analysis_status`` comes back ``failed``; 503 with no analyser installed,
    404 with no profile, 422 with neither website nor description."""
    return await social_service.analyze_profile(ctx, profile_id)


@router.post(
    "/profile/complete",
    response_model=SocialProfileResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def complete_profile(
    profile_id: str | None = _PROFILE_ID, ctx: RequestContext = Depends(request_context)
) -> SocialProfileResponse:
    """Finish onboarding. 422 ``social.profile_incomplete`` names what is missing."""
    return await social_service.complete_onboarding(ctx, profile_id)


@router.post(
    "/ideas/generate",
    response_model=SocialIdeaListResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def generate_ideas(
    body: GenerateIdeasRequest | None = None,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> SocialIdeaListResponse:
    """Generate ``count`` (1–12, default 6) new post ideas. 409 before onboarding
    is complete, 503 with no ideas writer, 502 when the run fails."""
    return await social_service.generate_ideas(ctx, body or GenerateIdeasRequest(), profile_id)


@router.get(
    "/ideas",
    response_model=SocialIdeaListResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def list_ideas(
    status: IdeaStatus | None = Query(default=None),
    platform: Platform | None = Query(default=None),
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> SocialIdeaListResponse:
    """One profile's ideas, newest first, optionally one status."""
    return await social_service.list_ideas(
        ctx, status=status, profile_id=profile_id, platform=platform
    )


@router.patch(
    "/ideas/{idea_id}",
    response_model=SocialIdeaResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def update_idea(
    idea_id: str,
    body: UpdateIdeaRequest,
    ctx: RequestContext = Depends(request_context),
) -> SocialIdeaResponse:
    """Approve / skip / reset an idea and/or edit its copy. A malformed or
    foreign id is a 404."""
    return await social_service.update_idea(ctx, idea_id, body)


@router.post(
    "/ideas/schedule",
    response_model=SocialIdeaListResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def schedule_ideas(
    body: ScheduleIdeasRequest,
    ctx: RequestContext = Depends(request_context),
) -> SocialIdeaListResponse:
    """Date approved ideas and add each to /calendar. 409 if any is not approved."""
    return await social_service.schedule_ideas(ctx, body)


@router.post(
    "/ideas/{idea_id}/unschedule",
    response_model=SocialIdeaResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def unschedule_idea(
    idea_id: str,
    ctx: RequestContext = Depends(request_context),
) -> SocialIdeaResponse:
    """Clear an idea's date and remove its /calendar event."""
    return await social_service.unschedule_idea(ctx, idea_id)
