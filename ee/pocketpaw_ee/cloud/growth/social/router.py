# ee/pocketpaw_ee/cloud/growth/social/router.py — FastAPI router for Growth ›
# Social, mounted under ``/api/v1`` beside the growth router (routes live at
# ``/api/v1/growth/social/...``). A thin shell over ``growth.social.service``.
# Every route carries the license gate, ``request_context`` and a per-route
# RBAC guard (pinned by the guard-coverage test in tests/cloud/growth/
# test_gate.py): GETs ``growth.read``, workspace-internal writes
# ``growth.write``, and connecting or disconnecting a social account, uploading
# media or posting (which reach an outside platform) ``growth.manage``. The
# media upload reads at most one byte past the size cap. The connect route builds the
# OAuth return URL from the caller's origin, validated against the CORS
# allowlist; it is never taken from the request body.

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace
from pocketpaw_ee.cloud.auth.social.service import frontend_base_url, safe_frontend_origin
from pocketpaw_ee.cloud.growth.social import service as social_service
from pocketpaw_ee.cloud.growth.social.domain import MAX_MEDIA_BYTES, IdeaStatus, Platform
from pocketpaw_ee.cloud.growth.social.dto import (
    CheckTelegramRequest,
    CheckTelegramResponse,
    ConnectSocialAccountRequest,
    ConnectSocialAccountResponse,
    CreateCharacterRequest,
    CreateSocialPostRequest,
    CreateSocialPostResponse,
    GenerateIdeasRequest,
    MakeMediaRequest,
    MakeMemeRequest,
    MemeFormatListResponse,
    RefreshPulseRequest,
    ScheduleIdeasRequest,
    SocialAccountListResponse,
    SocialChannelListResponse,
    SocialIdeaListResponse,
    SocialIdeaResponse,
    SocialMediaResponse,
    SocialPostListResponse,
    SocialProfileListResponse,
    SocialProfileResponse,
    SocialPulseResponse,
    UpdateIdeaRequest,
    UpsertProfileRequest,
    UsePulseSuggestionRequest,
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


@router.get(
    "/ideas/{idea_id}",
    response_model=SocialIdeaResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def get_idea(
    idea_id: str, ctx: RequestContext = Depends(request_context)
) -> SocialIdeaResponse:
    return await social_service.get_idea(ctx, idea_id)


@router.post(
    "/ideas/{idea_id}/media",
    response_model=SocialIdeaResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def make_media(
    idea_id: str,
    body: MakeMediaRequest,
    ctx: RequestContext = Depends(request_context),
) -> SocialIdeaResponse:
    """Draw a poster (SVG) or a ~10 s reel (HyperFrames HTML) for one idea, editable in /studio."""
    return await social_service.make_media(ctx, idea_id, body)


@router.get(
    "/meme-formats",
    response_model=MemeFormatListResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def list_meme_formats() -> MemeFormatListResponse:
    """The meme format templates Create offers (our own layouts, no third-party media)."""
    return social_service.list_meme_formats()


@router.post(
    "/characters",
    response_model=SocialProfileResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def create_character(
    body: CreateCharacterRequest,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> SocialProfileResponse:
    """Draw an original vector mascot for this social. 409 past the character limit."""
    return await social_service.create_character(ctx, body, profile_id)


@router.delete(
    "/characters/{character_id}",
    response_model=SocialProfileResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def delete_character(
    character_id: str,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> SocialProfileResponse:
    return await social_service.delete_character(ctx, character_id, profile_id)


@router.post(
    "/memes",
    response_model=SocialIdeaResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def make_meme(
    body: MakeMemeRequest,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> SocialIdeaResponse:
    """Draw a meme with a format and character and file it as a new Blitz idea."""
    return await social_service.make_meme(ctx, body, profile_id)


@router.get(
    "/pulse",
    response_model=SocialPulseResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def get_pulse(
    profile_id: str | None = _PROFILE_ID, ctx: RequestContext = Depends(request_context)
) -> SocialPulseResponse:
    """The social's latest market Pulse (empty before the first refresh)."""
    return await social_service.get_pulse(ctx, profile_id)


@router.post(
    "/pulse/refresh",
    response_model=SocialPulseResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def refresh_pulse(
    body: RefreshPulseRequest | None = None,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> SocialPulseResponse:
    """Read Reddit / X for this social's market and replace its Pulse (~1 min, in-request).
    503 with no provider, 409 before setup or within the refresh cooldown, 502 on no themes."""
    return await social_service.refresh_pulse(ctx, body or RefreshPulseRequest(), profile_id)


@router.post(
    "/pulse/use",
    response_model=SocialIdeaResponse,
    dependencies=[Depends(require_action_any_workspace("growth.write"))],
)
async def use_pulse_suggestion(
    body: UsePulseSuggestionRequest,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> SocialIdeaResponse:
    """File a theme's post suggestion as an idea: to Blitz (new) or straight to Content."""
    return await social_service.use_pulse_suggestion(ctx, body, profile_id)


def _caller_origin(request: Request) -> str:
    origin = (request.headers.get("origin") or "").strip()
    if origin and origin != "null":
        return origin.rstrip("/")
    parsed = urlsplit(request.headers.get("referer") or "")
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


@router.get(
    "/accounts",
    response_model=SocialAccountListResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def list_accounts(
    profile_id: str | None = _PROFILE_ID, ctx: RequestContext = Depends(request_context)
) -> SocialAccountListResponse:
    """The social's connected accounts and the platforms it can connect.
    ``configured`` is false when this deployment has no Postiz."""
    return await social_service.list_accounts(ctx, profile_id)


@router.post(
    "/accounts/connect",
    response_model=ConnectSocialAccountResponse,
    dependencies=[Depends(require_action_any_workspace("growth.manage"))],
)
async def connect_account(
    body: ConnectSocialAccountRequest,
    request: Request,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> ConnectSocialAccountResponse:
    """The authorize URL, or for Telegram a ``/connect`` code. 503 with no Postiz,
    422 for an unsupported or unset-up platform."""
    origin = safe_frontend_origin(_caller_origin(request)) or frontend_base_url()
    return await social_service.connect_account(ctx, profile_id, body, origin)


@router.post(
    "/accounts/telegram/check",
    response_model=CheckTelegramResponse,
    dependencies=[Depends(require_action_any_workspace("growth.manage"))],
)
async def check_telegram(
    body: CheckTelegramRequest,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> CheckTelegramResponse:
    """Finish a Telegram connect once ``/connect <code>`` was posted; else ``pending``."""
    return await social_service.check_telegram(ctx, profile_id, body)


@router.delete(
    "/accounts/{account_id}",
    status_code=204,
    dependencies=[Depends(require_action_any_workspace("growth.manage"))],
)
async def disconnect_account(
    account_id: str,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> Response:
    """Disconnect one account. 404 when it isn't this social's."""
    await social_service.disconnect_account(ctx, profile_id, account_id)
    return Response(status_code=204)


@router.get(
    "/accounts/{account_id}/channels",
    response_model=SocialChannelListResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def list_channels(
    account_id: str,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> SocialChannelListResponse:
    """The channels a post to this account can go to (Slack)."""
    return await social_service.list_channels(ctx, profile_id, account_id)


@router.post(
    "/media",
    response_model=SocialMediaResponse,
    dependencies=[Depends(require_action_any_workspace("growth.manage"))],
)
async def upload_media(
    file: UploadFile = File(...),
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> SocialMediaResponse:
    """Store one image or MP4 in the profile's Postiz org for a later post."""
    data = await file.read(MAX_MEDIA_BYTES + 1)
    return await social_service.upload_media(ctx, profile_id, data)


@router.post(
    "/posts",
    response_model=CreateSocialPostResponse,
    dependencies=[Depends(require_action_any_workspace("growth.manage"))],
)
async def create_post(
    body: CreateSocialPostRequest,
    profile_id: str | None = _PROFILE_ID,
    ctx: RequestContext = Depends(request_context),
) -> CreateSocialPostResponse:
    """Publish one post now (with thread parts and media) to each target account."""
    return await social_service.create_post(ctx, profile_id, body)


@router.get(
    "/posts",
    response_model=SocialPostListResponse,
    dependencies=[Depends(require_action_any_workspace("growth.read"))],
)
async def list_posts(
    profile_id: str | None = _PROFILE_ID,
    ids: str | None = Query(default=None, max_length=2000),
    ctx: RequestContext = Depends(request_context),
) -> SocialPostListResponse:
    """Posts from the last 7 days, newest first; ``ids`` narrows to those post ids."""
    return await social_service.list_posts(ctx, profile_id, ids)
