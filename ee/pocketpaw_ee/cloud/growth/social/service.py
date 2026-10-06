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
        status=doc.status,
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
        status=i.status,
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
            profile, body.count, await _recent_hooks(workspace_id, profile_key)
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
            status="new",
        )
        await idea.insert()
        docs.append(idea)
    if not docs:
        raise CloudError(502, "social.ideas_failed", "The ideas run returned no usable ideas")
    # no-event: Growth › Social has no realtime subscriber; Blitz re-fetches.
    return SocialIdeaListResponse(items=[_idea_to_response(_idea_to_domain(d)) for d in docs])


async def list_ideas(
    ctx: RequestContext, *, status: str | None = None, profile_id: str | None = None
) -> SocialIdeaListResponse:
    workspace_id = _require_workspace(ctx)
    doc = await _profile_doc(workspace_id, profile_id)
    if doc is None:
        return SocialIdeaListResponse(items=[])
    query: dict[str, Any] = {"workspace": workspace_id, "profile": str(doc.id)}
    if status is not None:
        query["status"] = status
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


__all__ = [
    "analyze_profile",
    "complete_onboarding",
    "create_profile",
    "list_profiles",
    "generate_ideas",
    "get_profile",
    "list_ideas",
    "update_idea",
    "upsert_profile",
]
