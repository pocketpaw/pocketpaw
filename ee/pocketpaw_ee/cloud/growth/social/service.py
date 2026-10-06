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
    AnalysisPatch,
    DescriptionResponse,
    GenerateIdeasRequest,
    MakeMediaRequest,
    ScheduleIdeasRequest,
    SocialAnalysisResponse,
    SocialIdeaListResponse,
    SocialIdeaResponse,
    SocialProfileListResponse,
    SocialProfileResponse,
    UpdateIdeaRequest,
    UpsertProfileRequest,
)
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as _IdeaDoc
from pocketpaw_ee.cloud.models.social_profile import SocialProfile as _ProfileDoc

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
    """Move an idea's review status and/or edit its copy."""
    body = UpdateIdeaRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    doc = await _fetch_idea_in_workspace(workspace_id, idea_id)
    for name in ("status", "hook", "on_screen_text", "caption", "script", "hashtags"):
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
    "get_idea",
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
