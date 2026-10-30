# ee/pocketpaw_ee/cloud/growth/social/service.py — the Growth › Social service
# and the SOLE owner of the SocialProfile and SocialIdea doc writes
# (service-is-repo; the import-linter "Growth" contract keeps the router, DTOs,
# domain and both agents off the doc classes).
#
# Tenancy: every read filters on ``workspace``; malformed, missing and
# cross-tenant profile and idea ids raise the same NotFound so existence never
# leaks. A workspace may hold several profiles (one per brand). Every profile
# route takes an optional ``profile_id``; without one it acts on the most
# recently updated profile, and ``upsert_profile`` creates the first. Ideas
# belong to one profile.
#
# A PUT may carry a hand-edited ``analysis``: only the editable fields sent are
# replaced (``pages_read`` / ``logo_url`` stay server-owned), ``analyzed_at`` is
# untouched, and a ``none`` / ``failed`` status becomes ``ready``.
#
# Analysis runs in-request, like ``growth.service.preview_icp``: 503 when no
# ``AnalyzeFn`` is installed, then 404 (no profile), then 422 (neither website
# nor description). Any fetch or model failure is saved as
# ``analysis_status="failed"`` with a short ``analysis_error`` and returned as a
# 200; a failed run leaves the previous ``analysis`` / ``analyzed_at`` in place.
# The result is written with one atomic ``$set`` of the analysis fields, so a
# PUT that lands during the 20–60 s run is not clobbered.
#
# Ideas: generating needs a completed onboarding (409 otherwise, including no
# profile) and an installed ``IdeasFn`` (503); a failed or empty run is a 502.
# Recent hooks are passed so the agent does not repeat itself. Writes carry
# ``# no-event:`` markers: Growth › Social has no realtime subscriber; the
# dashboard re-fetches.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    NotFound,
    ValidationError,
)
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.growth.social.domain import (
    DESCRIPTION_FIELDS,
    AnalysisOutcome,
    AnalysisRequest,
    SocialAnalysis,
    SocialIdea,
    SocialProfile,
)
from pocketpaw_ee.cloud.growth.social.dto import (
    AddPulseCompaniesRequest,
    AddPulseCompaniesResponse,
    AnalysisPatch,
    CharacterResponse,
    CreateCharacterRequest,
    DescriptionResponse,
    GenerateIdeasRequest,
    MakeMediaRequest,
    MakeMemeRequest,
    MemeFormatListResponse,
    MemeFormatResponse,
    PulseSourceModel,
    PulseThemeResponse,
    RefreshPulseRequest,
    ScheduleIdeasRequest,
    SocialAnalysisResponse,
    SocialIdeaListResponse,
    SocialIdeaResponse,
    SocialProfileListResponse,
    SocialProfileResponse,
    SocialPulseResponse,
    UpdateIdeaRequest,
    UpsertProfileRequest,
    UsePulseSuggestionRequest,
)
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as _IdeaDoc
from pocketpaw_ee.cloud.models.social_profile import SocialProfile as _ProfileDoc
from pocketpaw_ee.cloud.models.social_pulse import SocialPulse as _PulseDoc

logger = logging.getLogger(__name__)

RECENT_HOOKS_LIMIT = 40
IDEA_LIST_LIMIT = 500
PROFILE_LIST_LIMIT = 100
_NULLABLE_FIELDS = ("website", "team_size", "monthly_revenue", "role", "business_model", "category")
_ANALYSIS_TEXTS = ("summary", "product", "audience", "problem", "tone")
_ANALYSIS_LISTS = (
    "benefits",
    "differentiators",
    "competitors",
    "avoid",
    "content_pillars",
    "hooks",
    "pages_read",
)


def _require_workspace(ctx: RequestContext) -> str:
    if not ctx.workspace_id:
        raise Forbidden("social.no_workspace", "Active workspace required for growth operations")
    return ctx.workspace_id


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _analysis_from_raw(raw: Any) -> SocialAnalysis | None:
    if not isinstance(raw, dict):
        return None
    fields: dict[str, Any] = {}
    for name in _ANALYSIS_TEXTS:
        value = raw.get(name)
        fields[name] = value if isinstance(value, str) else ""
    for name in _ANALYSIS_LISTS:
        value = raw.get(name) if isinstance(raw.get(name), list) else []
        fields[name] = tuple(v for v in value if isinstance(v, str))
    logo = raw.get("logo_url")
    fields["logo_url"] = logo if isinstance(logo, str) and logo else None
    return SocialAnalysis(**fields)


def _analysis_to_raw(analysis: SocialAnalysis) -> dict[str, Any]:
    raw: dict[str, Any] = {name: getattr(analysis, name) for name in _ANALYSIS_TEXTS}
    raw.update({name: list(getattr(analysis, name)) for name in _ANALYSIS_LISTS})
    raw["logo_url"] = analysis.logo_url
    return raw


def _profile_to_domain(doc: _ProfileDoc) -> SocialProfile:
    stored = doc.description or {}
    return SocialProfile(
        id=str(doc.id),
        workspace_id=doc.workspace,
        owner_name=doc.owner_name or "",
        company_name=doc.company_name or "",
        website=doc.website,
        description={key: str(stored.get(key) or "") for key in DESCRIPTION_FIELDS},
        team_size=doc.team_size,
        monthly_revenue=doc.monthly_revenue,
        role=doc.role,
        business_model=doc.business_model,
        category=doc.category,
        analysis_status=doc.analysis_status or "none",
        analysis_error=doc.analysis_error,
        analysis=_analysis_from_raw(doc.analysis),
        analyzed_at=doc.analyzed_at,
        onboarding_completed_at=doc.onboarding_completed_at,
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
        characters=tuple(dict(c) for c in (doc.characters or [])),
    )


def _profile_to_response(p: SocialProfile) -> SocialProfileResponse:
    analysis = None
    if p.analysis is not None:
        analysis = SocialAnalysisResponse.model_validate(_analysis_to_raw(p.analysis))
    return SocialProfileResponse(
        id=p.id,
        workspace_id=p.workspace_id,
        owner_name=p.owner_name,
        company_name=p.company_name,
        website=p.website,
        description=DescriptionResponse(**p.description),
        team_size=p.team_size,
        monthly_revenue=p.monthly_revenue,
        role=p.role,
        business_model=p.business_model,
        category=p.category,
        analysis_status=p.analysis_status,
        analysis_error=p.analysis_error,
        analysis=analysis,
        analyzed_at=iso_utc(p.analyzed_at),
        onboarding_completed_at=iso_utc(p.onboarding_completed_at),
        created_at=iso_utc(p.created_at),
        updated_at=iso_utc(p.updated_at),
        characters=[CharacterResponse(**c) for c in p.characters],
    )


def _idea_to_domain(doc: _IdeaDoc) -> SocialIdea:
    return SocialIdea(
        id=str(doc.id),
        workspace_id=doc.workspace,
        format=doc.format,
        hook=doc.hook,
        on_screen_text=doc.on_screen_text,
        caption=doc.caption,
        why=doc.why,
        script=tuple(doc.script or ()),
        hashtags=tuple(doc.hashtags or ()),
        platform=doc.platform,
        subreddit=doc.subreddit,
        status=doc.status,
        scheduled_at=doc.scheduled_at,
        calendar_event_id=doc.calendar_event_id,
        poster_svg=doc.poster_svg,
        reel_html=doc.reel_html,
        media_choice=doc.media_choice,
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
    )


def _idea_to_response(i: SocialIdea) -> SocialIdeaResponse:
    return SocialIdeaResponse(
        id=i.id,
        workspace_id=i.workspace_id,
        format=i.format,
        hook=i.hook,
        on_screen_text=i.on_screen_text,
        caption=i.caption,
        why=i.why,
        script=list(i.script),
        hashtags=list(i.hashtags),
        platform=i.platform,
        subreddit=i.subreddit,
        status=i.status,
        scheduled_at=iso_utc(i.scheduled_at),
        calendar_event_id=i.calendar_event_id,
        poster_svg=i.poster_svg,
        reel_html=i.reel_html,
        media_choice=i.media_choice,
        created_at=iso_utc(i.created_at),
        updated_at=iso_utc(i.updated_at),
    )


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


async def _profile_doc(workspace_id: str, profile_id: str | None = None) -> _ProfileDoc | None:
    if profile_id is None:
        docs = (
            await _ProfileDoc.find({"workspace": workspace_id})
            .sort([("updatedAt", -1), ("_id", -1)])
            .limit(1)
            .to_list()
        )
        return docs[0] if docs else None
    try:
        oid = PydanticObjectId(profile_id)
    except Exception:  # noqa: BLE001
        return None
    return await _ProfileDoc.find_one({"_id": oid, "workspace": workspace_id})


async def _require_profile_doc(workspace_id: str, profile_id: str | None = None) -> _ProfileDoc:
    doc = await _profile_doc(workspace_id, profile_id)
    if doc is None:
        raise NotFound("social_profile")
    return doc


async def list_profiles(ctx: RequestContext) -> SocialProfileListResponse:
    docs = (
        await _ProfileDoc.find({"workspace": _require_workspace(ctx)})
        .sort([("createdAt", 1), ("_id", 1)])
        .limit(PROFILE_LIST_LIMIT)
        .to_list()
    )
    return SocialProfileListResponse(
        items=[_profile_to_response(_profile_to_domain(d)) for d in docs]
    )


async def create_profile(ctx: RequestContext) -> SocialProfileResponse:
    """Start a new, empty profile; the setup wizard fills it in."""
    doc = _ProfileDoc(workspace=_require_workspace(ctx))
    await doc.insert()
    # no-event: Growth › Social has no realtime subscriber; the wizard re-fetches.
    return _profile_to_response(_profile_to_domain(doc))


async def get_profile(ctx: RequestContext, profile_id: str | None = None) -> SocialProfileResponse:
    doc = await _require_profile_doc(_require_workspace(ctx), profile_id)
    return _profile_to_response(_profile_to_domain(doc))


def _apply_upsert(doc: _ProfileDoc, body: UpsertProfileRequest) -> None:
    sent = body.model_fields_set
    for name in ("owner_name", "company_name"):
        if name in sent:
            setattr(doc, name, getattr(body, name) or "")
    for name in _NULLABLE_FIELDS:
        if name in sent:
            setattr(doc, name, getattr(body, name) or None)
    if "description" in sent and body.description is not None:
        merged = {key: str((doc.description or {}).get(key) or "") for key in DESCRIPTION_FIELDS}
        for key in body.description.model_fields_set:
            merged[key] = (getattr(body.description, key) or "").strip()
        doc.description = merged
    if "analysis" in sent and body.analysis is not None:
        _apply_analysis_edit(doc, body.analysis)


def _apply_analysis_edit(doc: _ProfileDoc, patch: AnalysisPatch) -> None:
    current = _analysis_from_raw(doc.analysis) or SocialAnalysis()
    raw = _analysis_to_raw(current)
    for name in patch.model_fields_set:
        value = getattr(patch, name)
        if value is not None:
            raw[name] = list(value) if isinstance(value, list) else value
    doc.analysis = raw
    if doc.analysis_status in ("none", "failed"):
        doc.analysis_status = "ready"
        doc.analysis_error = None


async def upsert_profile(
    ctx: RequestContext, body: UpsertProfileRequest, profile_id: str | None = None
) -> SocialProfileResponse:
    """Apply the sent fields. With no ``profile_id`` and no profile yet, create
    the workspace's first one."""
    body = UpsertProfileRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    if profile_id is not None:
        doc = await _require_profile_doc(workspace_id, profile_id)
    else:
        doc = await _profile_doc(workspace_id)
    if doc is None:
        doc = _ProfileDoc(workspace=workspace_id)
        _apply_upsert(doc, body)
        await doc.insert()
    else:
        _apply_upsert(doc, body)
        await doc.save()
    # no-event: Growth › Social has no realtime subscriber; the wizard re-fetches.
    return _profile_to_response(_profile_to_domain(doc))


async def analyze_profile(
    ctx: RequestContext, profile_id: str | None = None
) -> SocialProfileResponse:
    """Read the website (if any) and run the analyst, in-request."""
    from pocketpaw_ee.cloud.growth.social import analyst as social_analyst

    workspace_id = _require_workspace(ctx)
    analyze_fn = social_analyst.resolve_analyze_fn()
    if analyze_fn is None:
        raise CloudError(
            503,
            "social.analyzer_unavailable",
            "Website analysis is not configured on this deployment",
        )
    doc = await _require_profile_doc(workspace_id, profile_id)
    profile = _profile_to_domain(doc)
    if not profile.website and not profile.has_description():
        raise ValidationError(
            "social.nothing_to_analyze",
            "Add a website or at least one description field before analysing",
        )

    request = AnalysisRequest(
        workspace_id=workspace_id,
        company_name=profile.company_name,
        website=profile.website,
        description=dict(profile.description),
    )
    try:
        outcome = await analyze_fn(request)
    except Exception:  # noqa: BLE001
        logger.warning("growth social: analysis raised for ws=%s", workspace_id, exc_info=True)
        outcome = AnalysisOutcome(error="The analysis failed. Try again shortly.")

    now = datetime.now(UTC)
    if outcome.analysis is not None:
        update: dict[str, Any] = {
            "analysis_status": "ready",
            "analysis_error": None,
            "analysis": _analysis_to_raw(outcome.analysis),
            "analyzed_at": now,
        }
    else:
        update = {
            "analysis_status": "failed",
            "analysis_error": (outcome.error or "The analysis failed.")[:300],
        }
    update["updatedAt"] = now
    await _ProfileDoc.find_one({"_id": doc.id, "workspace": workspace_id}).update({"$set": update})
    # no-event: Growth › Social has no realtime subscriber; the wizard re-fetches.
    return _profile_to_response(
        _profile_to_domain(await _require_profile_doc(workspace_id, str(doc.id)))
    )


async def complete_onboarding(
    ctx: RequestContext, profile_id: str | None = None
) -> SocialProfileResponse:
    """Stamp ``onboarding_completed_at`` once every required field is set."""
    workspace_id = _require_workspace(ctx)
    doc = await _require_profile_doc(workspace_id, profile_id)
    missing = _profile_to_domain(doc).missing_for_completion()
    if missing:
        raise ValidationError(
            "social.profile_incomplete", "Still needed before finishing: " + ", ".join(missing)
        )
    if doc.onboarding_completed_at is None:
        doc.onboarding_completed_at = datetime.now(UTC)
        await doc.save()
    # no-event: Growth › Social has no realtime subscriber; the wizard re-fetches.
    return _profile_to_response(_profile_to_domain(doc))


# ---------------------------------------------------------------------------
# Ideas
# ---------------------------------------------------------------------------


async def _fetch_idea_in_workspace(workspace_id: str, idea_id: str) -> _IdeaDoc:
    try:
        oid = PydanticObjectId(idea_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("social_idea", idea_id) from exc
    doc = await _IdeaDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("social_idea", idea_id)
    return doc


async def _recent_hooks(workspace_id: str, profile: str) -> list[str]:
    docs = (
        await _IdeaDoc.find({"workspace": workspace_id, "profile": profile})
        .sort([("createdAt", -1), ("_id", -1)])
        .limit(RECENT_HOOKS_LIMIT)
        .to_list()
    )
    return [d.hook for d in docs if d.hook]


async def generate_ideas(
    ctx: RequestContext, body: GenerateIdeasRequest, profile_id: str | None = None
) -> SocialIdeaListResponse:
    """Ask the ideas agent for ``count`` new ideas and store them as ``new``."""
    from pocketpaw_ee.cloud.growth.researcher import ResearchUnavailable
    from pocketpaw_ee.cloud.growth.social import ideas as social_ideas

    body = GenerateIdeasRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    ideas_fn = social_ideas.resolve_ideas_fn()
    if ideas_fn is None:
        raise CloudError(
            503, "social.ideas_unavailable", "Idea generation is not configured on this deployment"
        )
    doc = await _profile_doc(workspace_id, profile_id)
    if doc is None or doc.onboarding_completed_at is None:
        raise ConflictError(
            "social.onboarding_incomplete", "Finish the Social setup before generating ideas"
        )

    profile = _profile_to_domain(doc)
    profile_key = str(doc.id)
    try:
        generated = await ideas_fn(
            profile,
            body.count,
            await _recent_hooks(workspace_id, profile_key),
            platform=body.platform,
            pulse=await _pulse_themes(workspace_id, profile_key),
        )
    except ResearchUnavailable as exc:
        raise CloudError(502, "social.ideas_failed", f"Idea generation failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("growth social: ideas run failed for ws=%s", workspace_id, exc_info=True)
        raise CloudError(502, "social.ideas_failed", "The ideas run failed") from exc

    docs: list[_IdeaDoc] = []
    for item in list(generated)[: body.count]:
        idea = _IdeaDoc(
            workspace=workspace_id,
            profile=profile_key,
            format=item.format,
            hook=item.hook,
            on_screen_text=item.on_screen_text,
            caption=item.caption,
            why=item.why,
            script=list(item.script),
            hashtags=list(item.hashtags),
            platform=item.platform,
            subreddit=item.subreddit,
            status="new",
        )
        await idea.insert()
        docs.append(idea)
    if not docs:
        raise CloudError(502, "social.ideas_failed", "The ideas run returned no usable ideas")
    # no-event: Growth › Social has no realtime subscriber; Blitz re-fetches.
    return SocialIdeaListResponse(items=[_idea_to_response(_idea_to_domain(d)) for d in docs])


async def list_ideas(
    ctx: RequestContext,
    *,
    status: str | None = None,
    profile_id: str | None = None,
    platform: str | None = None,
) -> SocialIdeaListResponse:
    workspace_id = _require_workspace(ctx)
    doc = await _profile_doc(workspace_id, profile_id)
    if doc is None:
        return SocialIdeaListResponse(items=[])
    query: dict[str, Any] = {"workspace": workspace_id, "profile": str(doc.id)}
    if status is not None:
        query["status"] = status
    if platform is not None:
        query["platform"] = platform
    docs = (
        await _IdeaDoc.find(query)
        .sort([("createdAt", -1), ("_id", -1)])
        .limit(IDEA_LIST_LIMIT)
        .to_list()
    )
    return SocialIdeaListResponse(items=[_idea_to_response(_idea_to_domain(d)) for d in docs])


async def update_idea(
    ctx: RequestContext, idea_id: str, body: UpdateIdeaRequest
) -> SocialIdeaResponse:
    """Move an idea's review status, edit its copy, pick which media goes with the
    post (``media_choice``: poster or reel), and/or replace its poster with
    an edited one (from /studio/vector), sanitised like a generated poster."""
    from pocketpaw_ee.cloud.growth.social.ideas import MAX_EDITED_POSTER_CHARS, clean_svg

    body = UpdateIdeaRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_idea_in_workspace(workspace_id, idea_id)
    if body.poster_svg is not None:
        poster = clean_svg(body.poster_svg, MAX_EDITED_POSTER_CHARS)
        if not poster:
            raise ValidationError("social.poster_invalid", "That poster is not a usable SVG")
        doc.poster_svg = poster
    fields = ("status", "hook", "on_screen_text", "caption", "script", "hashtags", "media_choice")
    for name in fields:
        value = getattr(body, name)
        if value is not None:
            setattr(doc, name, value)
    await doc.save()
    # no-event: Growth › Social has no realtime subscriber; Blitz re-fetches.
    return _idea_to_response(_idea_to_domain(doc))


async def get_idea(ctx: RequestContext, idea_id: str) -> SocialIdeaResponse:
    doc = await _fetch_idea_in_workspace(_require_workspace(ctx), idea_id)
    return _idea_to_response(_idea_to_domain(doc))


async def make_media(
    ctx: RequestContext, idea_id: str, body: MakeMediaRequest
) -> SocialIdeaResponse:
    """Have the ideas agent draw a poster (SVG) or a reel (HyperFrames HTML) for one idea."""
    from pocketpaw_ee.cloud.growth.researcher import ResearchUnavailable
    from pocketpaw_ee.cloud.growth.social import ideas as social_ideas

    body = MakeMediaRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    media_fn = social_ideas.resolve_media_fn()
    if media_fn is None:
        raise CloudError(
            503, "social.media_unavailable", "Poster and reel drafts are not configured here"
        )
    doc = await _fetch_idea_in_workspace(workspace_id, idea_id)
    profile_doc = await _profile_doc(workspace_id, doc.profile or None)
    if profile_doc is None:
        raise NotFound("social_profile")
    try:
        made = await media_fn(_profile_to_domain(profile_doc), _idea_to_domain(doc), body.kind)
    except ResearchUnavailable as exc:
        raise CloudError(502, "social.media_failed", f"Drawing the {body.kind} failed") from exc
    if not made:
        raise CloudError(502, "social.media_failed", f"The {body.kind} came back unusable")
    if body.kind == "poster":
        doc.poster_svg = made
    else:
        doc.reel_html = made
    await doc.save()
    # no-event: Growth › Social has no realtime subscriber; the card re-renders from the response.
    return _idea_to_response(_idea_to_domain(doc))


def list_meme_formats() -> MemeFormatListResponse:
    from pocketpaw_ee.cloud.growth.social.memes import MEME_FORMATS

    return MemeFormatListResponse(
        items=[MemeFormatResponse(id=i, name=n, layout=lay) for i, n, lay in MEME_FORMATS]
    )


async def create_character(
    ctx: RequestContext, body: CreateCharacterRequest, profile_id: str | None = None
) -> SocialProfileResponse:
    """Have the agent draw an original vector mascot and keep it on the profile."""
    import secrets

    from pocketpaw_ee.cloud.growth.researcher import ResearchUnavailable
    from pocketpaw_ee.cloud.growth.social import memes
    from pocketpaw_ee.cloud.growth.social.ideas import clean_svg

    body = CreateCharacterRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    draw = memes.resolve_character_fn()
    if draw is None:
        raise CloudError(503, "social.characters_unavailable", "Characters are not configured here")
    doc = await _require_profile_doc(workspace_id, profile_id)
    if len(doc.characters or []) >= memes.MAX_CHARACTERS:
        raise ConflictError(
            "social.character_limit", f"A social can keep {memes.MAX_CHARACTERS} characters"
        )
    try:
        svg = clean_svg(
            await draw(_profile_to_domain(doc), body.name.strip(), body.description.strip())
        )
    except ResearchUnavailable as exc:
        raise CloudError(502, "social.character_failed", "Drawing the character failed") from exc
    if not svg:
        raise CloudError(502, "social.character_failed", "The character came back unusable")
    character = {
        "id": secrets.token_hex(6),
        "name": body.name.strip() or "Character",
        "description": body.description.strip(),
        "svg": svg,
    }
    await _ProfileDoc.find_one({"_id": doc.id, "workspace": workspace_id}).update(
        {"$push": {"characters": character}, "$set": {"updatedAt": datetime.now(UTC)}}
    )
    # no-event: Growth › Social has no realtime subscriber; Create re-renders from the response.
    return await get_profile(ctx, str(doc.id))


async def delete_character(
    ctx: RequestContext, character_id: str, profile_id: str | None = None
) -> SocialProfileResponse:
    workspace_id = _require_workspace(ctx)
    doc = await _require_profile_doc(workspace_id, profile_id)
    await _ProfileDoc.find_one({"_id": doc.id, "workspace": workspace_id}).update(
        {"$pull": {"characters": {"id": character_id}}, "$set": {"updatedAt": datetime.now(UTC)}}
    )
    # no-event: Growth › Social has no realtime subscriber; Create re-renders from the response.
    return await get_profile(ctx, str(doc.id))


async def make_meme(
    ctx: RequestContext, body: MakeMemeRequest, profile_id: str | None = None
) -> SocialIdeaResponse:
    """Draw a meme (character + format layout as SVG) and file it as a new Blitz idea."""
    from pocketpaw_ee.cloud.growth.researcher import ResearchUnavailable
    from pocketpaw_ee.cloud.growth.social import memes
    from pocketpaw_ee.cloud.growth.social.ideas import clean_svg

    body = MakeMemeRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    make = memes.resolve_meme_fn()
    if make is None:
        raise CloudError(503, "social.memes_unavailable", "Memes are not configured here")
    if body.format and body.format not in memes.MEME_FORMAT_IDS:
        raise ValidationError("social.unknown_format", f"Unknown meme format {body.format!r}")
    doc = await _profile_doc(workspace_id, profile_id)
    if doc is None or doc.onboarding_completed_at is None:
        raise ConflictError(
            "social.onboarding_incomplete", "Finish the Social setup before making memes"
        )
    character = None
    if body.character_id:
        character = next(
            (c for c in doc.characters or [] if c.get("id") == body.character_id), None
        )
        if character is None:
            raise NotFound("social_character", body.character_id)
    try:
        made = await make(
            _profile_to_domain(doc),
            character,
            body.format,
            body.mention_business,
            body.prompt,
            body.platform,
        )
    except ResearchUnavailable as exc:
        raise CloudError(502, "social.meme_failed", "Making the meme failed") from exc
    meme_svg = clean_svg(made.svg) if made is not None else ""
    if not meme_svg:
        raise CloudError(502, "social.meme_failed", "The meme came back unusable")
    idea = _IdeaDoc(
        workspace=workspace_id,
        profile=str(doc.id),
        format="meme",
        hook=made.hook,
        caption=made.caption,
        why=made.why,
        platform=body.platform,
        poster_svg=meme_svg,
        status="new",
    )
    await idea.insert()
    # no-event: Growth › Social has no realtime subscriber; Blitz re-fetches.
    return _idea_to_response(_idea_to_domain(idea))


# ---------------------------------------------------------------------------
# Pulse
# ---------------------------------------------------------------------------

PULSE_COOLDOWN_SECONDS = 600


async def _pulse_doc(workspace_id: str, profile: str) -> _PulseDoc | None:
    return await _PulseDoc.find_one({"workspace": workspace_id, "profile": profile})


async def _pulse_themes(workspace_id: str, profile: str) -> list[dict[str, Any]]:
    doc = await _pulse_doc(workspace_id, profile)
    return list(doc.themes) if doc else []


def _pulse_response(doc: _PulseDoc | None) -> SocialPulseResponse:
    if doc is None:
        return SocialPulseResponse(sources=[], themes=[], refreshed_at=None)
    return SocialPulseResponse(
        sources=[PulseSourceModel(**s) for s in doc.sources],
        themes=[PulseThemeResponse(**t) for t in doc.themes],
        refreshed_at=iso_utc(doc.refreshed_at),
    )


async def get_pulse(ctx: RequestContext, profile_id: str | None = None) -> SocialPulseResponse:
    workspace_id = _require_workspace(ctx)
    profile = await _require_profile_doc(workspace_id, profile_id)
    return _pulse_response(await _pulse_doc(workspace_id, str(profile.id)))


def _pulse_sources(body_sources: Any, doc: _PulseDoc | None) -> tuple[Any, ...] | None:
    from pocketpaw_ee.cloud.growth.social import pulse as social_pulse

    if body_sources is not None:
        cleaned = [social_pulse.clean_source(s.platform, s.kind, s.value) for s in body_sources]
        return tuple(s for s in cleaned if s is not None)
    if doc and doc.sources:
        return tuple(social_pulse.PulseSource(**s) for s in doc.sources)
    return None


async def _rebuild_pulse(
    doc: _PulseDoc, brief: Any, sources: tuple[Any, ...] | None, *, manual: bool
) -> _PulseDoc:
    """Run the Pulse for ``brief`` and replace ``doc``'s themes. Evidence not seen in the previous
    Pulse is marked ``new``. Raises CloudError (503 no provider, 502 failed or no themes)."""
    from pocketpaw_ee.cloud.growth.researcher import ResearchUnavailable
    from pocketpaw_ee.cloud.growth.social import pulse as social_pulse

    build = social_pulse.resolve_pulse_fn()
    if build is None:
        raise CloudError(503, "social.pulse_unavailable", "Market research is not configured here")
    try:
        result = await build(brief, sources or None)
    except social_pulse.PulseUnavailable as exc:
        raise CloudError(503, "social.pulse_unavailable", str(exc)) from exc
    except ResearchUnavailable as exc:
        raise CloudError(502, "social.pulse_failed", "Reading the market failed") from exc
    if not result.themes:
        raise CloudError(502, "social.pulse_failed", "No clear themes came back; try other sources")
    seen = {ev.get("url") for t in doc.themes for ev in t.get("evidence") or []}
    themes = []
    for theme in result.themes:
        evidence = [
            {**ev, "new": bool(seen) and ev.get("url") not in seen} for ev in theme["evidence"]
        ]
        themes.append({**theme, "evidence": evidence})
    now = datetime.now(UTC)
    doc.sources = [s.__dict__ for s in result.sources]
    doc.themes = themes
    doc.refreshed_at = now
    if manual:
        doc.manual_refreshed_at = now
    await doc.save()
    return doc


def _check_cooldown(doc: _PulseDoc | None, sources: tuple[Any, ...] | None, changed: bool) -> None:
    if doc is None or doc.refreshed_at is None or changed:
        return
    last = doc.refreshed_at if doc.refreshed_at.tzinfo else doc.refreshed_at.replace(tzinfo=UTC)
    if (datetime.now(UTC) - last).total_seconds() < PULSE_COOLDOWN_SECONDS:
        raise ConflictError(
            "social.pulse_cooldown", "The Pulse was just refreshed; try again in a few minutes"
        )


async def refresh_pulse(
    ctx: RequestContext, body: RefreshPulseRequest, profile_id: str | None = None
) -> SocialPulseResponse:
    """Read the market (Reddit / X) and replace this social's Pulse. In-request, ~1 min."""
    from pocketpaw_ee.cloud.growth.social import pulse as social_pulse

    body = RefreshPulseRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    if social_pulse.resolve_pulse_fn() is None:
        raise CloudError(503, "social.pulse_unavailable", "Market research is not configured here")
    profile_doc = await _profile_doc(workspace_id, profile_id)
    if profile_doc is None or profile_doc.onboarding_completed_at is None:
        raise ConflictError("social.onboarding_incomplete", "Finish the Social setup first")
    key = str(profile_doc.id)
    doc = await _pulse_doc(workspace_id, key)
    sources = _pulse_sources(body.sources, doc)
    changed = body.sources is not None and [s.__dict__ for s in sources or ()] != (
        doc.sources if doc else []
    )
    _check_cooldown(doc, sources, changed)
    doc = doc or _PulseDoc(workspace=workspace_id, profile=key)
    brief = social_pulse.social_brief(_profile_to_domain(profile_doc))
    doc = await _rebuild_pulse(doc, brief, sources, manual=True)
    # no-event: Growth › Social has no realtime subscriber; the Pulse page re-renders.
    return _pulse_response(doc)


# --- Hunt (ICP) market pulse: same Pulse, keyed "icp:<icp_id>", read for outreach ---


def _hunt_key(icp_id: str) -> str:
    return f"icp:{icp_id}"


def _icp_brief(workspace_id: str, icp: Any) -> Any:
    from pocketpaw_ee.cloud.growth.social import pulse as social_pulse

    return social_pulse.hunt_brief(
        workspace_id, icp.name, icp.criteria, icp.geography, icp.exclusions
    )


async def get_hunt_pulse(ctx: RequestContext, icp_id: str) -> SocialPulseResponse:
    from pocketpaw_ee.cloud.growth import service as growth_service

    workspace_id = _require_workspace(ctx)
    await growth_service.get_icp(ctx, icp_id)
    return _pulse_response(await _pulse_doc(workspace_id, _hunt_key(icp_id)))


async def refresh_hunt_pulse(
    ctx: RequestContext, icp_id: str, body: RefreshPulseRequest
) -> SocialPulseResponse:
    """Read what people matching this ICP say on Reddit / X: themes with why they matter for the
    hunt, outreach angles, and companies the posts name as having the need."""
    from pocketpaw_ee.cloud.growth import service as growth_service

    body = RefreshPulseRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    icp = await growth_service.get_icp(ctx, icp_id)
    key = _hunt_key(icp_id)
    doc = await _pulse_doc(workspace_id, key)
    sources = _pulse_sources(body.sources, doc)
    changed = body.sources is not None and [s.__dict__ for s in sources or ()] != (
        doc.sources if doc else []
    )
    _check_cooldown(doc, sources, changed)
    doc = doc or _PulseDoc(workspace=workspace_id, profile=key)
    doc = await _rebuild_pulse(doc, _icp_brief(workspace_id, icp), sources, manual=True)
    # no-event: growth has no realtime subscriber in v1; the Hunt page re-renders.
    return _pulse_response(doc)


async def add_pulse_companies(
    ctx: RequestContext, icp_id: str, body: AddPulseCompaniesRequest
) -> AddPulseCompaniesResponse:
    """File the companies a hunt theme names as prospects (company-level only, with the source
    post as evidence). Companies with no stated website are skipped and reported."""
    from pocketpaw_ee.cloud.growth import service as growth_service
    from pocketpaw_ee.cloud.growth.dto import CreateProspectRequest

    body = AddPulseCompaniesRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    icp = await growth_service.get_icp(ctx, icp_id)
    doc = await _pulse_doc(workspace_id, _hunt_key(icp_id))
    themes = doc.themes if doc else []
    if body.theme >= len(themes):
        raise NotFound("social_pulse_theme", str(body.theme))
    theme = themes[body.theme]
    added: list[str] = []
    skipped: list[str] = []
    for company in theme.get("companies") or []:
        if not company.get("domain"):
            skipped.append(company.get("name", ""))
            continue
        await growth_service.upsert_by_domain(
            workspace_id,
            CreateProspectRequest(
                company=company.get("name", ""),
                domain=company["domain"],
                source="discovery",
                project_id=icp.project_id,
                icp_id=icp.id,
                research_brief=(
                    f"From the {icp.name} market pulse, {theme.get('title', '')}: "
                    f"{company.get('reason', '')}"
                )[:2000],
                source_urls=[company["url"]] if company.get("url") else [],
            ),
        )
        added.append(company.get("name", ""))
    # no-event: upsert_by_domain owns the prospect writes; the Hunt page re-renders.
    return AddPulseCompaniesResponse(added=added, skipped=skipped)


# --- Daily refresh ---

PULSE_STALE_HOURS = 20
PULSE_ACTIVE_DAYS = 7


async def refresh_due_pulses(now: datetime | None = None) -> dict[str, int]:
    """Re-read every Pulse a person refreshed in the last week that is older than a day, with its
    saved sources. One failure never stops the sweep. Runs from the growth worker's cron."""
    from datetime import timedelta

    from pocketpaw_ee.cloud.growth import service as growth_service
    from pocketpaw_ee.cloud.growth.social import pulse as social_pulse

    now = now or datetime.now(UTC)
    if social_pulse.resolve_pulse_fn() is None:
        return {"due": 0, "refreshed": 0, "failed": 0}
    # global-read: the sweep runs for every workspace; each doc carries its own tenant.
    docs = await _PulseDoc.find(
        {
            "manual_refreshed_at": {"$gte": now - timedelta(days=PULSE_ACTIVE_DAYS)},
            "refreshed_at": {"$lte": now - timedelta(hours=PULSE_STALE_HOURS)},
        }
    ).to_list()
    refreshed = failed = 0
    for doc in docs:
        try:
            if doc.profile.startswith("icp:"):
                icp = await growth_service.get_icp_system(doc.workspace, doc.profile[4:])
                brief = _icp_brief(doc.workspace, icp)
            else:
                profile = await _require_profile_doc(doc.workspace, doc.profile)
                brief = social_pulse.social_brief(_profile_to_domain(profile))
            await _rebuild_pulse(doc, brief, _pulse_sources(None, doc), manual=False)
            refreshed += 1
        except Exception:  # noqa: BLE001
            failed += 1
            logger.warning("pulse sweep: %s failed", doc.profile, exc_info=True)
    logger.info("pulse sweep: due=%d refreshed=%d failed=%d", len(docs), refreshed, failed)
    return {"due": len(docs), "refreshed": refreshed, "failed": failed}


async def use_pulse_suggestion(
    ctx: RequestContext, body: UsePulseSuggestionRequest, profile_id: str | None = None
) -> SocialIdeaResponse:
    """File one of a theme's post suggestions as an idea (to Blitz, or approved to Content)."""
    body = UsePulseSuggestionRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    profile = await _require_profile_doc(workspace_id, profile_id)
    doc = await _pulse_doc(workspace_id, str(profile.id))
    themes = doc.themes if doc else []
    if body.theme >= len(themes):
        raise NotFound("social_pulse_theme", str(body.theme))
    theme = themes[body.theme]
    suggestions = theme.get("suggestions") or []
    if body.suggestion >= len(suggestions):
        raise NotFound("social_pulse_suggestion", str(body.suggestion))
    s = suggestions[body.suggestion]
    reddit = s.get("platform") == "reddit"
    idea = _IdeaDoc(
        workspace=workspace_id,
        profile=str(profile.id),
        format="reddit_post" if reddit else "x_post",
        hook=s.get("hook") or theme.get("title", ""),
        caption=s.get("caption") or "",
        why=f"From your market pulse, {theme.get('title', '')}: {theme.get('why', '')}"[:400],
        platform="reddit" if reddit else "x",
        subreddit=s.get("subreddit") or "",
        status=body.status,
    )
    await idea.insert()
    # no-event: Growth › Social has no realtime subscriber; Blitz / Content re-fetch.
    return _idea_to_response(_idea_to_domain(idea))


SOCIAL_CALENDAR_ID = "growth-social"
_PLATFORM_NAMES = {"x": "X", "reddit": "Reddit"}


def _calendar_ctx(ctx: RequestContext, workspace_id: str) -> Any:
    from pocketpaw_ee.calendar._context import RequestContext as CalendarContext

    return CalendarContext(workspace_id=workspace_id, user_id=ctx.user_id or "")


def _event_title(doc: _IdeaDoc) -> str:
    where = _PLATFORM_NAMES.get(doc.platform, "Social")
    if doc.platform == "reddit" and doc.subreddit:
        where = f"Reddit r/{doc.subreddit}"
    return f"{where} post: {doc.hook}"[:500]


def _event_description(doc: _IdeaDoc) -> str:
    parts = [doc.caption, *doc.script]
    if doc.hashtags:
        parts.append(" ".join(doc.hashtags))
    return "\n\n".join(p for p in parts if p)[:5000]


async def schedule_ideas(ctx: RequestContext, body: ScheduleIdeasRequest) -> SocialIdeaListResponse:
    """Give approved ideas a date and mark each with a /calendar event (created,
    or moved when the idea was already scheduled). Nothing is posted."""
    from datetime import timedelta

    from pocketpaw_ee.calendar import service as calendar_service
    from pocketpaw_ee.calendar.dto import CreateEventRequest, UpdateEventRequest

    body = ScheduleIdeasRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    docs = [await _fetch_idea_in_workspace(workspace_id, item.idea_id) for item in body.items]
    not_approved = [d for d in docs if d.status != "approved"]
    if not_approved:
        raise ConflictError("social.idea_not_approved", "Only approved ideas can be scheduled")

    cal_ctx = _calendar_ctx(ctx, workspace_id)
    length = timedelta(minutes=body.duration_minutes)
    for doc, item in zip(docs, body.items, strict=True):
        starts = (
            item.scheduled_at if item.scheduled_at.tzinfo else item.scheduled_at.replace(tzinfo=UTC)
        )
        moved = False
        if doc.calendar_event_id:
            try:
                await calendar_service.update_event(
                    cal_ctx,
                    doc.calendar_event_id,
                    UpdateEventRequest(
                        starts_at=starts, ends_at=starts + length, timezone=body.timezone
                    ),
                )
                moved = True
            except NotFound:
                moved = False
        if not moved:
            event = await calendar_service.create_event(
                cal_ctx,
                CreateEventRequest(
                    calendar_id=SOCIAL_CALENDAR_ID,
                    title=_event_title(doc),
                    description=_event_description(doc),
                    starts_at=starts,
                    ends_at=starts + length,
                    timezone=body.timezone,
                ),
            )
            doc.calendar_event_id = event.id
        doc.scheduled_at = starts
        await doc.save()
    # no-event: Growth › Social has no realtime subscriber; /calendar got its own event.
    return SocialIdeaListResponse(items=[_idea_to_response(_idea_to_domain(d)) for d in docs])


async def unschedule_idea(ctx: RequestContext, idea_id: str) -> SocialIdeaResponse:
    """Clear an idea's date and delete its /calendar event."""
    from pocketpaw_ee.calendar import service as calendar_service

    workspace_id = _require_workspace(ctx)
    doc = await _fetch_idea_in_workspace(workspace_id, idea_id)
    if doc.calendar_event_id:
        try:
            await calendar_service.delete_event(
                _calendar_ctx(ctx, workspace_id), doc.calendar_event_id
            )
        except NotFound:
            pass
    doc.scheduled_at = None
    doc.calendar_event_id = ""
    await doc.save()
    # no-event: Growth › Social has no realtime subscriber; /calendar dropped its event.
    return _idea_to_response(_idea_to_domain(doc))


__all__ = [
    "analyze_profile",
    "complete_onboarding",
    "create_profile",
    "create_character",
    "delete_character",
    "get_idea",
    "add_pulse_companies",
    "get_hunt_pulse",
    "get_pulse",
    "refresh_due_pulses",
    "refresh_hunt_pulse",
    "refresh_pulse",
    "use_pulse_suggestion",
    "list_meme_formats",
    "make_meme",
    "list_profiles",
    "make_media",
    "schedule_ideas",
    "unschedule_idea",
    "generate_ideas",
    "get_profile",
    "list_ideas",
    "update_idea",
    "upsert_profile",
]
