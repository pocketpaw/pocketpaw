# ee/pocketpaw_ee/cloud/growth/social/service.py — the Growth › Social service
# and the SOLE owner of the SocialProfile, SocialIdea, SocialPulse,
# SocialPostizOrg, SocialPostMetric, RepoMetric, ReplyDraft and SocialLink doc
# writes (service-is-repo; the import-linter "Growth" contract keeps the routers,
# DTOs, domain, the agents and the Postiz client off the doc classes).
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
# untouched, and a ``none`` / ``failed`` status becomes ``ready``. The product
# registry fields (``active``, ``weekly_post_quota``, ``github_repos``) are set
# only when sent; an explicit ``null`` resets one to its default.
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
#
# Daily X drafts: ``run_daily_drafts`` (the growth worker's 05:30 UTC cron, the
# one cross-workspace read here) and ``draft_daily`` (the on-demand route) both
# go through ``_draft_daily``: an active, onboarded profile gets at most its
# ``weekly_post_quota`` daily drafts per rolling 7 days and two per UTC day,
# built on the last 24 h of its GitHub changes, for X only. Every draft must
# pass ``ideas.check_draft`` (one retry carries the reasons); the rest are
# dropped. Survivors are stored ``new`` with ``source="daily"`` and a
# ``suggested_at`` slot from ``DAILY_SLOT_HOURS_UTC``. Nothing here posts.
#
# Repo metrics: ``run_repo_metrics`` (the 06:30 UTC cron, a cross-workspace read)
# stores one ``RepoMetric`` per (workspace, repo, yesterday UTC) for every active
# profile's valid ``github_repos``, reading each repo once per run. A rerun the same
# day refreshes that day's row in place, never a second row, and keeps traffic
# an earlier run stored when its own traffic read failed. ``repo_metrics`` reads
# one workspace's rows back. ``metrics_summary`` (the Today strip) is a pure read of
# yesterday's X posts with their latest snapshot and clicks, plus 14 days per repo.
#
# Accounts: each profile's social accounts live in its own Postiz org, created
# on the first connect and never on a read. Its API key is stored encrypted, and
# the key is checked before Postiz is called, because org creation is not
# idempotent. A disconnect first confirms the id is in that profile's org.
# Telegram connects by code: ``connect_account`` stores Postiz's state on the
# org row, and ``check_telegram`` sweeps the bot's pending ``/connect`` messages
# onto every unexpired row whose code they name (across workspaces, so confirming
# the batch never drops another tenant's) before finishing the caller's connect.
# Posting checks the media (public URL, one video or up to four images), the
# idea and every target (owned by the profile's org, a postable platform, a
# channel where the platform needs one) before Postiz is asked to publish, so a
# bad target never leaves a half-sent post. Media rides the first value entry,
# thread parts follow; a video is dropped for platforms outside VIDEO_PLATFORMS.
# A post with ``scheduled_at`` goes to Postiz as ``type: "schedule"`` at that
# UTC time (Postiz's own workflow holds it until then); without one it is
# ``type: "now"``. Only after Postiz returns created posts are their ids added to
# the linked idea's ``post_ids`` (workspace-filtered write), so a failed post
# leaves the idea untouched.
# Media is stored on the deployment's public bucket (not in Postiz, whose storage
# is local or R2-only), content-addressed under the workspace and profile. The
# posts list never creates an org.
# Post metrics: ``run_post_metrics`` (the 07:00 UTC cron) records each X idea's
# live post from Postiz, then stores append-only X metric snapshots at 1/3/7/28
# days through the capped, read-only ``x_api`` reader. Nothing here writes to X.
# Reply cards: ``run_reply_drafts`` (05:45 UTC cron) and ``draft_replies`` draft X
# replies via ``replies``; a person posts each by hand from its intent link.
#
# Tracked links: when a post names an idea and its body holds a URL, the first
# URL becomes a SocialLink (reused per idea + URL), leaves the body, and goes out
# as a ``Link: <base>/<code>`` thread part appended after the others (a post that
# is only the URL keeps the tracked link in its body). Postiz posts parts as
# replies (X and Slack both have ``comment()`` at v2.25.0). Posts with no idea
# or no URL are sent unchanged. ``record_click`` (the public ``/go/{code}``)
# checks the code pattern before reading, ``$inc``s the total and the UTC day
# bucket unless a preview bot asked, and stores nothing about the requester.

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.crypto import decrypt, encrypt
from pocketpaw_ee.cloud._core.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    NotFound,
    ValidationError,
)
from pocketpaw_ee.cloud._core.public_url import public_base_url
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.growth.social.changes import Change, changes_text, resolve_changes_fn
from pocketpaw_ee.cloud.growth.social.domain import (
    CHANNEL_PLATFORMS,
    DAILY_DRAFTS_PER_DAY,
    DAILY_SLOT_HOURS_UTC,
    DESCRIPTION_FIELDS,
    MEDIA_EXTENSIONS,
    METRIC_AGE_BUCKETS,
    POSTABLE_PLATFORMS,
    REPLY_CARDS_PER_DAY_DEFAULT,
    SOCIAL_ACCOUNT_PLATFORMS,
    VIDEO_PLATFORMS,
    AnalysisOutcome,
    AnalysisRequest,
    GeneratedIdea,
    PostMetrics,
    ReplyCard,
    SocialAnalysis,
    SocialIdea,
    SocialProfile,
    is_public_media_url,
    media_kind,
)
from pocketpaw_ee.cloud.growth.social.dto import (
    AddPulseCompaniesRequest,
    AddPulseCompaniesResponse,
    AnalysisPatch,
    CharacterResponse,
    CheckTelegramRequest,
    CheckTelegramResponse,
    ConnectSocialAccountRequest,
    ConnectSocialAccountResponse,
    CreateCharacterRequest,
    CreateSocialPostRequest,
    CreateSocialPostResponse,
    DescriptionResponse,
    GenerateIdeasRequest,
    MakeMediaRequest,
    MakeMemeRequest,
    MemeFormatListResponse,
    MemeFormatResponse,
    MetricsPostResponse,
    MetricsRepoResponse,
    MetricsSummaryResponse,
    PulseSourceModel,
    PulseThemeResponse,
    RefreshPulseRequest,
    ReplyDraftListResponse,
    ReplyDraftResponse,
    RepoStarsPointResponse,
    ScheduleIdeasRequest,
    SocialAccountListResponse,
    SocialAccountPlatformResponse,
    SocialAccountResponse,
    SocialAnalysisResponse,
    SocialChannelListResponse,
    SocialChannelResponse,
    SocialIdeaListResponse,
    SocialIdeaResponse,
    SocialMediaResponse,
    SocialPostCreated,
    SocialPostListResponse,
    SocialPostResponse,
    SocialProfileListResponse,
    SocialProfileResponse,
    SocialPulseResponse,
    TelegramConnectCode,
    UpdateIdeaRequest,
    UpdateReplyRequest,
    UpsertProfileRequest,
    UsePulseSuggestionRequest,
)
from pocketpaw_ee.cloud.growth.social.links import (
    extract_urls,
    is_preview_bot,
    is_web_url,
    new_code,
    valid_code,
)
from pocketpaw_ee.cloud.growth.social.metrics import METRIC_FIELDS
from pocketpaw_ee.cloud.growth.social.postiz import (
    PostizClient,
    PostizError,
    PostizPlatformUnavailable,
    resolve_postiz,
    resolve_telegram_updates,
)
from pocketpaw_ee.cloud.growth.social.repo_metrics import (
    RepoSnapshot,
    is_repo_slug,
    resolve_repo_metrics_fn,
)
from pocketpaw_ee.cloud.models.reply_draft import ReplyDraft as _ReplyDoc
from pocketpaw_ee.cloud.models.repo_metric import RepoMetric as _RepoMetricDoc
from pocketpaw_ee.cloud.models.social_idea import SocialIdea as _IdeaDoc
from pocketpaw_ee.cloud.models.social_link import SocialLink as _LinkDoc
from pocketpaw_ee.cloud.models.social_post_metric import SocialPostMetric as _MetricDoc
from pocketpaw_ee.cloud.models.social_postiz_org import SocialPostizOrg as _PostizOrgDoc
from pocketpaw_ee.cloud.models.social_profile import SocialProfile as _ProfileDoc
from pocketpaw_ee.cloud.models.social_pulse import SocialPulse as _PulseDoc
from pocketpaw_ee.sites.public_assets import kind_for_mime, max_bytes_for_mime, sniff_media_mime

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
        active=doc.active,
        weekly_post_quota=doc.weekly_post_quota,
        github_repos=tuple(doc.github_repos or ()),
        reply_watch_handles=tuple(doc.reply_watch_handles or ()),
        reply_queries=tuple(doc.reply_queries or ()),
        reply_cards_per_day=doc.reply_cards_per_day,
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
        active=p.active,
        weekly_post_quota=p.weekly_post_quota,
        github_repos=list(p.github_repos),
        reply_watch_handles=list(p.reply_watch_handles),
        reply_queries=list(p.reply_queries),
        reply_cards_per_day=p.reply_cards_per_day,
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
        source=doc.source,
        suggested_at=doc.suggested_at,
        post_ids=tuple(doc.post_ids or ()),
        published_url=doc.published_url,
        published_at=doc.published_at,
        x_post_id=doc.x_post_id,
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
        source=i.source,
        suggested_at=iso_utc(i.suggested_at),
        post_ids=list(i.post_ids),
        published_url=i.published_url,
        published_at=iso_utc(i.published_at),
        x_post_id=i.x_post_id,
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
    # Not in the loops above: ``False`` / ``0`` are real values, not "clear".
    if "active" in sent:
        doc.active = bool(body.active)
    if "weekly_post_quota" in sent:
        doc.weekly_post_quota = 5 if body.weekly_post_quota is None else body.weekly_post_quota
    if "github_repos" in sent:
        doc.github_repos = list(body.github_repos or [])
    if "reply_watch_handles" in sent:
        doc.reply_watch_handles = list(body.reply_watch_handles or [])
    if "reply_queries" in sent:
        doc.reply_queries = list(body.reply_queries or [])
    if "reply_cards_per_day" in sent:
        per_day = body.reply_cards_per_day
        doc.reply_cards_per_day = REPLY_CARDS_PER_DAY_DEFAULT if per_day is None else per_day


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
    source: str | None = None,
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
    if source is not None:
        # Rows from before ``source`` existed carry no key; they are manual.
        query["source"] = "daily" if source == "daily" else {"$ne": "daily"}
    docs = (
        await _IdeaDoc.find(query)
        .sort([("createdAt", -1), ("_id", -1)])
        .limit(IDEA_LIST_LIMIT)
        .to_list()
    )
    return SocialIdeaListResponse(items=[_idea_to_response(_idea_to_domain(d)) for d in docs])


# ---------------------------------------------------------------------------
# Daily X drafts
# ---------------------------------------------------------------------------


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _daily_count(workspace_id: str, profile: str, since: datetime) -> int:
    return await _IdeaDoc.find(
        {
            "workspace": workspace_id,
            "profile": profile,
            "source": "daily",
            "createdAt": {"$gte": since},
        }
    ).count()


async def _free_slots(workspace_id: str, profile: str, now: datetime) -> list[datetime]:
    """Today's and tomorrow's slot times still ahead of ``now`` that no daily draft holds."""
    today = _day_start(now)
    held = await _IdeaDoc.find(
        {
            "workspace": workspace_id,
            "profile": profile,
            "source": "daily",
            "suggested_at": {"$gte": today},
        }
    ).to_list()
    taken = {_aware(d.suggested_at) for d in held if d.suggested_at is not None}
    slots = [
        today + timedelta(days=day, hours=hour) for day in (0, 1) for hour in DAILY_SLOT_HOURS_UTC
    ]
    return [slot for slot in slots if slot > now and slot not in taken]


async def _gather_changes(repos: list[str], since: datetime) -> list[Change]:
    changes_fn = resolve_changes_fn()
    if changes_fn is None or not repos:
        return []
    try:
        return list(await changes_fn(repos, since=since))
    except Exception:  # noqa: BLE001
        logger.warning("daily drafts: reading GitHub changes failed", exc_info=True)
        return []


async def _draft_daily(
    workspace_id: str, profile_id: str, now: datetime
) -> tuple[list[_IdeaDoc], int]:
    """The daily drafts this profile is owed right now, and how many drafts were dropped."""
    from pocketpaw_ee.cloud.growth.social import ideas as social_ideas

    ideas_fn = social_ideas.resolve_ideas_fn()
    doc = await _require_profile_doc(workspace_id, profile_id)
    if ideas_fn is None or not doc.active or doc.onboarding_completed_at is None:
        return [], 0
    profile_key = str(doc.id)
    left_week = doc.weekly_post_quota - await _daily_count(
        workspace_id, profile_key, now - timedelta(days=7)
    )
    left_today = DAILY_DRAFTS_PER_DAY - await _daily_count(
        workspace_id, profile_key, _day_start(now)
    )
    n = min(left_week, left_today)
    if n <= 0:
        return [], 0

    profile = _profile_to_domain(doc)
    changes = await _gather_changes(list(doc.github_repos or ()), now - timedelta(hours=24))
    source_text = changes_text(changes)
    hooks = await _recent_hooks(workspace_id, profile_key)
    pulse = await _pulse_themes(workspace_id, profile_key)
    kept: list[GeneratedIdea] = []
    reasons: list[str] = []
    dropped = 0
    retry_note: str | None = None
    for _attempt in range(2):  # the first call, then at most one retry
        generated = await ideas_fn(
            profile,
            n - len(kept),
            hooks,
            platform="x",
            pulse=pulse,
            changes=changes,
            retry_note=retry_note,
        )
        for idea in generated:
            problems = social_ideas.check_draft(idea, source_text)
            if problems:
                dropped += 1
                reasons += [r for r in problems if r not in reasons]
            elif len(kept) < n:
                kept.append(idea)
        if len(kept) >= n:
            break
        retry_note = "; ".join(reasons) or None
    if dropped:
        logger.info("daily drafts: dropped %d draft(s) for profile=%s", dropped, profile_key)

    slots = await _free_slots(workspace_id, profile_key, now)
    docs: list[_IdeaDoc] = []
    for item, slot in zip(kept, slots, strict=False):
        idea_doc = _IdeaDoc(
            workspace=workspace_id,
            profile=profile_key,
            format=item.format,
            hook=item.hook,
            on_screen_text=item.on_screen_text,
            caption=item.caption,
            why=item.why,
            script=list(item.script),
            hashtags=list(item.hashtags),
            platform="x",
            status="new",
            source="daily",
            suggested_at=slot,
        )
        await idea_doc.insert()
        docs.append(idea_doc)
    return docs, dropped


async def draft_daily_for_profile(
    workspace_id: str, profile_id: str, *, now: datetime | None = None
) -> list[SocialIdea]:
    """Draft this profile's X posts for today, within its weekly quota and two a day.

    Skips (returns []) an inactive or not-yet-onboarded profile and one whose quota is used.
    Drafts that break ``check_draft`` are dropped; one retry is asked for with the reasons.
    Every draft is stored as ``new``: nothing here posts."""
    docs, _ = await _draft_daily(workspace_id, profile_id, now or datetime.now(UTC))
    # no-event: Growth › Social has no realtime subscriber; Blitz re-fetches.
    return [_idea_to_domain(d) for d in docs]


async def run_daily_drafts(now: datetime | None = None) -> dict[str, int]:
    """Draft today's X posts for every active, onboarded profile. One failing profile never
    stops the rest. Runs from the growth worker's cron."""
    from pocketpaw_ee.cloud.growth.social import ideas as social_ideas

    now = now or datetime.now(UTC)
    counts = {"profiles": 0, "drafted": 0, "dropped": 0, "failed": 0}
    if social_ideas.resolve_ideas_fn() is None:
        return counts
    # global-read: the sweep runs for every workspace; each doc carries its own tenant.
    docs = await _ProfileDoc.find(
        {"active": True, "onboarding_completed_at": {"$ne": None}}
    ).to_list()
    counts["profiles"] = len(docs)
    for doc in docs:
        try:
            made, dropped = await _draft_daily(doc.workspace, str(doc.id), now)
            counts["drafted"] += len(made)
            counts["dropped"] += dropped
        except Exception:  # noqa: BLE001
            counts["failed"] += 1
            logger.warning("daily drafts: profile=%s failed", doc.id, exc_info=True)
    logger.info(
        "daily drafts: profiles=%d drafted=%d dropped=%d failed=%d",
        counts["profiles"],
        counts["drafted"],
        counts["dropped"],
        counts["failed"],
    )
    # no-event: Growth › Social has no realtime subscriber; Blitz re-fetches.
    return counts


_SNAPSHOT_FIELDS = (
    "stars",
    "forks",
    "views",
    "uniques",
    "clones",
    "clone_uniques",
    "traffic_available",
)


async def _store_metric(workspace_id: str, snap: RepoSnapshot, now: datetime) -> None:
    """Write the day's row, or refresh it in place when a rerun finds one. A rerun that
    could not read traffic keeps the traffic an earlier run stored and refreshes the rest."""
    fields: dict[str, Any] = {k: getattr(snap, k) for k in _SNAPSHOT_FIELDS}
    fields["referrers"] = [dict(r) for r in snap.referrers]
    fields["captured_at"] = now
    key = {"workspace": workspace_id, "repo": snap.repo, "day": snap.day}
    doc = await _RepoMetricDoc.find_one(key)
    if doc is None:
        try:
            await _RepoMetricDoc(**key, **fields).insert()
            return
        except DuplicateKeyError:  # a parallel run inserted it first
            doc = await _RepoMetricDoc.find_one(key)
            if doc is None:
                raise
    if doc.traffic_available and not snap.traffic_available:
        fields = {k: fields[k] for k in ("stars", "forks", "captured_at")}
    for k, v in fields.items():
        setattr(doc, k, v)
    await doc.save()


async def run_repo_metrics(now: datetime | None = None) -> dict[str, int]:
    """Store yesterday's (UTC) stars, forks and traffic for every active profile's repos.

    One row per (workspace, repo, day); a rerun refreshes it. Each repo is read once per run
    and one failing repo never stops the rest. Runs from the growth worker's cron."""
    now = _aware(now or datetime.now(UTC))
    counts = {"repos": 0, "stored": 0, "failed": 0}
    metrics_fn = resolve_repo_metrics_fn()
    if metrics_fn is None:
        return counts
    day = (now - timedelta(days=1)).date().isoformat()
    # global-read: the sweep runs for every workspace; each doc carries its own tenant.
    docs = await _ProfileDoc.find({"active": True}).to_list()
    pairs: set[tuple[str, str]] = set()
    for doc in docs:
        for repo in doc.github_repos or ():
            if is_repo_slug(repo):
                pairs.add((doc.workspace, repo))
            else:
                logger.warning("repo metrics: skipping invalid repo slug %r", repo)
    counts["repos"] = len(pairs)
    snaps: dict[str, RepoSnapshot] = {}
    for workspace_id, repo in sorted(pairs):
        try:
            if repo not in snaps:
                snaps[repo] = await metrics_fn(repo, day=day)
            await _store_metric(workspace_id, snaps[repo], now)
            counts["stored"] += 1
        except Exception:  # noqa: BLE001
            counts["failed"] += 1
            logger.warning("repo metrics: %s failed", repo, exc_info=True)
    logger.info(
        "repo metrics: day=%s repos=%d stored=%d failed=%d",
        day,
        counts["repos"],
        counts["stored"],
        counts["failed"],
    )
    # no-event: Growth › Social has no realtime subscriber; readers re-fetch.
    return counts


async def repo_metrics(workspace_id: str, repo: str, since: date) -> list[RepoSnapshot]:
    """This workspace's stored days for one repo from ``since`` (inclusive), oldest first."""
    docs = (
        await _RepoMetricDoc.find(
            {"workspace": workspace_id, "repo": repo, "day": {"$gte": since.isoformat()}}
        )
        .sort("day")
        .to_list()
    )
    return [
        RepoSnapshot(
            repo=d.repo,
            day=d.day,
            referrers=tuple(d.referrers),
            **{k: getattr(d, k) for k in _SNAPSHOT_FIELDS},
        )
        for d in docs
    ]


SUMMARY_REPO_DAYS = 14


def _repo_summary(repo: str, rows: list[RepoSnapshot], today: date) -> MetricsRepoResponse:
    if not rows:
        return MetricsRepoResponse(repo=repo)
    latest = rows[-1]
    week_ago = (date.fromisoformat(latest.day) - timedelta(days=7)).isoformat()
    base = next((r for r in rows if r.day == week_ago), None)
    week_start = (today - timedelta(days=7)).isoformat()
    return MetricsRepoResponse(
        repo=repo,
        traffic_available=latest.traffic_available,
        uniques_week=sum(r.uniques for r in rows if r.day >= week_start),
        stars_week_delta=latest.stars - base.stars if base is not None else 0,
        stars=[RepoStarsPointResponse(day=r.day, stars=r.stars) for r in rows],
    )


async def metrics_summary(
    ctx: RequestContext, profile_id: str | None = None, now: datetime | None = None
) -> MetricsSummaryResponse:
    """The Today strip: the profile's X posts published yesterday (UTC) with their latest
    snapshot and tracked-link clicks, and its repos' last 14 days. A pure read."""
    workspace_id = _require_workspace(ctx)
    doc = await _profile_doc(workspace_id, profile_id)
    if doc is None:
        return MetricsSummaryResponse()
    today_start = _day_start(_aware(now or datetime.now(UTC)).astimezone(UTC))
    yesterday_start = today_start - timedelta(days=1)
    summary_query = {"workspace": workspace_id, "profile": str(doc.id), "platform": "x"}
    published = {"$gte": yesterday_start, "$lt": today_start}
    ideas = (
        await _IdeaDoc.find({**summary_query, "published_at": published})
        .sort([("published_at", 1), ("_id", 1)])
        .to_list()
    )
    ids = [str(d.id) for d in ideas]
    snaps = await latest_metrics(workspace_id, ids)
    clicks = await link_clicks(workspace_id, ids)
    posts = []
    for d in ideas:
        m = snaps.get(str(d.id))
        posts.append(
            MetricsPostResponse(
                idea_id=str(d.id),
                hook=d.hook,
                published_url=d.published_url,
                published_at=_aware(d.published_at),
                impressions=m.impressions if m else 0,
                likes=m.likes if m else 0,
                replies=m.replies if m else 0,
                reposts=m.reposts if m else 0,
                clicks=clicks.get(str(d.id), 0),
            )
        )
    today = today_start.date()
    since = today - timedelta(days=SUMMARY_REPO_DAYS)
    repos = [
        _repo_summary(repo, await repo_metrics(workspace_id, repo, since), today)
        for repo in doc.github_repos or ()
        if is_repo_slug(repo)
    ]
    return MetricsSummaryResponse(posts=posts, repos=repos)


async def draft_daily(ctx: RequestContext, profile_id: str | None = None) -> SocialIdeaListResponse:
    """Run today's X drafting for one profile now. 503 with no ideas writer, 409 before
    onboarding or while the profile is inactive, 502 when the run fails; an empty list when
    the quota is used up."""
    from pocketpaw_ee.cloud.growth.researcher import ResearchUnavailable
    from pocketpaw_ee.cloud.growth.social import ideas as social_ideas

    workspace_id = _require_workspace(ctx)
    if social_ideas.resolve_ideas_fn() is None:
        raise CloudError(
            503, "social.ideas_unavailable", "Idea generation is not configured on this deployment"
        )
    doc = await _profile_doc(workspace_id, profile_id)
    if doc is None:
        raise NotFound("social_profile")
    if doc.onboarding_completed_at is None:
        raise ConflictError(
            "social.onboarding_incomplete", "Finish the Social setup before drafting posts"
        )
    if not doc.active:
        raise ConflictError(
            "social.profile_inactive", "Turn this product on before drafting its daily posts"
        )
    try:
        made = await draft_daily_for_profile(workspace_id, str(doc.id))
    except ResearchUnavailable as exc:
        raise CloudError(502, "social.ideas_failed", f"Idea generation failed: {exc}") from exc
    except CloudError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily drafts: run failed for ws=%s", workspace_id, exc_info=True)
        raise CloudError(502, "social.ideas_failed", "The drafting run failed") from exc
    # no-event: Growth › Social has no realtime subscriber; Blitz re-fetches.
    return SocialIdeaListResponse(items=[_idea_to_response(i) for i in made])


# ---------------------------------------------------------------------------
# Reply cards (X): drafted only; a person posts each by hand from its intent link
# ---------------------------------------------------------------------------

REPLY_LIST_LIMIT = 200
# Cards made this recently block their post from a second card before any insert
# (the posts read are at most 24 h old); the unique index is the backstop.
REPLY_SEEN_WINDOW = timedelta(days=2)


def _reply_to_domain(doc: _ReplyDoc) -> ReplyCard:
    return ReplyCard(
        id=str(doc.id),
        workspace_id=doc.workspace,
        profile_id=doc.profile,
        target_post_id=doc.target_post_id,
        platform=doc.platform,
        target_url=doc.target_url,
        author=doc.author,
        context=doc.context,
        draft_text=doc.draft_text,
        intent_url=doc.intent_url,
        why=doc.why,
        status=doc.status,
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
    )


def _reply_to_response(r: ReplyCard) -> ReplyDraftResponse:
    return ReplyDraftResponse(
        id=r.id,
        workspace_id=r.workspace_id,
        profile_id=r.profile_id,
        platform=r.platform,
        target_post_id=r.target_post_id,
        target_url=r.target_url,
        author=r.author,
        context=r.context,
        draft_text=r.draft_text,
        intent_url=r.intent_url,
        why=r.why,
        status=r.status,
        created_at=iso_utc(r.created_at),
        updated_at=iso_utc(r.updated_at),
    )


async def _own_x_handles(workspace_id: str, profile: str) -> set[str]:
    """The profile's connected X usernames (a read of its Postiz org), lowercased, so
    its own posts get no card. Empty when Postiz is off or the read fails."""
    postiz = resolve_postiz()
    if postiz is None:
        return set()
    try:
        org = await _postiz_org(workspace_id, profile)
        rows = await postiz.list_integrations(decrypt(org.api_key_enc)) if org else []
    except Exception:  # noqa: BLE001
        logger.warning("reply drafts: reading connected accounts failed", exc_info=True)
        return set()
    return {
        str(r["profile"]).lstrip("@").lower()
        for r in rows
        if isinstance(r, dict) and r.get("identifier") == "x" and r.get("profile")
    }


async def _draft_replies(
    workspace_id: str, profile_id: str, now: datetime
) -> tuple[list[_ReplyDoc], int]:
    """The reply cards this profile is owed right now, and how many drafts were dropped."""
    from pocketpaw_ee.cloud.growth.social import replies as social_replies

    draft_fn = social_replies.resolve_reply_draft_fn()
    doc = await _require_profile_doc(workspace_id, profile_id)
    handles = list(doc.reply_watch_handles or ())
    queries = list(doc.reply_queries or ())
    if draft_fn is None or not doc.active or doc.onboarding_completed_at is None:
        return [], 0
    if not (handles or queries):
        return [], 0
    profile_key = str(doc.id)
    scope = {"workspace": workspace_id, "profile": profile_key}
    left = (
        doc.reply_cards_per_day
        - await _ReplyDoc.find({**scope, "createdAt": {"$gte": _day_start(now)}}).count()
    )
    if left <= 0:
        return [], 0
    since_seen = now - REPLY_SEEN_WINDOW
    recent = await _ReplyDoc.find({**scope, "createdAt": {"$gte": since_seen}}).to_list()
    candidates = await social_replies.find_candidates(
        workspace_id,
        handles,
        queries,
        now=now,
        own_handles=await _own_x_handles(workspace_id, profile_key),
        skip_ids={d.target_post_id for d in recent},
        limit=left,
    )
    if not candidates:
        return [], 0

    changes = await _gather_changes(list(doc.github_repos or ()), now - timedelta(hours=24))
    generated = await draft_fn(_profile_to_domain(doc), candidates, changes=changes)
    targets = {c.post_id: c for c in candidates}
    docs: list[_ReplyDoc] = []
    dropped = 0
    for reply in generated:
        target = targets.pop(reply.post_id, None)
        if target is None:
            continue
        source = social_replies.reply_source_text(target.text, changes)
        if social_replies.check_reply(reply.text, source):
            dropped += 1
            continue
        card = _ReplyDoc(
            workspace=workspace_id,
            profile=profile_key,
            platform="x",
            target_post_id=target.post_id,
            target_url=target.url,
            author=target.author,
            context=target.text,
            draft_text=reply.text,
            intent_url=social_replies.intent_url(target.post_id, reply.text),
            why=reply.why,
            status="new",
            source_text=source,
        )
        try:
            await card.insert()
        except DuplicateKeyError:
            continue  # another run carded this post first; a post never gets two cards
        docs.append(card)
    if dropped:
        logger.info("reply drafts: dropped %d draft(s) for profile=%s", dropped, profile_key)
    return docs, dropped


async def draft_replies_for_profile(
    workspace_id: str, profile_id: str, *, now: datetime | None = None
) -> list[ReplyCard]:
    """Draft this profile's reply cards now, within its ``reply_cards_per_day``.

    Skips (returns []) an inactive or not-yet-onboarded profile and one with no watch
    handle or query. Drafts that break ``replies.check_reply`` are dropped. Every card
    is stored as ``new``: nothing here posts."""
    docs, _ = await _draft_replies(workspace_id, profile_id, now or datetime.now(UTC))
    # no-event: Growth › Social has no realtime subscriber; the reply list re-fetches.
    return [_reply_to_domain(d) for d in docs]


async def run_reply_drafts(now: datetime | None = None) -> dict[str, int]:
    """Draft reply cards for every active, onboarded profile with a watch handle or query.
    One failing profile never stops the rest. Runs from the growth worker's cron."""
    from pocketpaw_ee.cloud.growth.social import replies as social_replies

    now = now or datetime.now(UTC)
    counts = {"profiles": 0, "drafted": 0, "dropped": 0, "failed": 0}
    if social_replies.resolve_reply_draft_fn() is None:
        return counts
    # global-read: the sweep runs for every workspace; each doc carries its own tenant.
    docs = await _ProfileDoc.find(
        {
            "active": True,
            "onboarding_completed_at": {"$ne": None},
            "$or": [
                {"reply_watch_handles.0": {"$exists": True}},
                {"reply_queries.0": {"$exists": True}},
            ],
        }
    ).to_list()
    counts["profiles"] = len(docs)
    for doc in docs:
        try:
            made, dropped = await _draft_replies(doc.workspace, str(doc.id), now)
            counts["drafted"] += len(made)
            counts["dropped"] += dropped
        except Exception:  # noqa: BLE001
            counts["failed"] += 1
            logger.warning("reply drafts: profile=%s failed", doc.id, exc_info=True)
    logger.info(
        "reply drafts: profiles=%d drafted=%d dropped=%d failed=%d",
        counts["profiles"],
        counts["drafted"],
        counts["dropped"],
        counts["failed"],
    )
    # no-event: Growth › Social has no realtime subscriber; the reply list re-fetches.
    return counts


async def draft_replies(
    ctx: RequestContext, profile_id: str | None = None
) -> ReplyDraftListResponse:
    """Draft this product's reply cards now. 503 with no reply drafter, 404 with no
    profile, 409 before onboarding, while inactive or with nothing to watch, 502 when
    the run fails; an empty list once today's cards are used up."""
    from pocketpaw_ee.cloud.growth.researcher import ResearchUnavailable
    from pocketpaw_ee.cloud.growth.social import replies as social_replies

    workspace_id = _require_workspace(ctx)
    if social_replies.resolve_reply_draft_fn() is None:
        raise CloudError(
            503, "social.replies_unavailable", "Reply drafting is not configured on this deployment"
        )
    doc = await _require_profile_doc(workspace_id, profile_id)
    if doc.onboarding_completed_at is None:
        raise ConflictError(
            "social.onboarding_incomplete", "Finish the Social setup before drafting replies"
        )
    if not doc.active:
        raise ConflictError(
            "social.profile_inactive", "Turn this product on before drafting its replies"
        )
    if not (doc.reply_watch_handles or doc.reply_queries):
        raise ConflictError(
            "social.no_reply_sources", "Add an X handle to watch or a search to draft replies"
        )
    try:
        made = await draft_replies_for_profile(workspace_id, str(doc.id))
    except ResearchUnavailable as exc:
        raise CloudError(502, "social.replies_failed", f"Reply drafting failed: {exc}") from exc
    except CloudError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("reply drafts: run failed for ws=%s", workspace_id, exc_info=True)
        raise CloudError(502, "social.replies_failed", "The reply drafting run failed") from exc
    # no-event: Growth › Social has no realtime subscriber; the reply list re-fetches.
    return ReplyDraftListResponse(items=[_reply_to_response(r) for r in made])


async def list_replies(
    ctx: RequestContext, *, profile_id: str | None = None, status: str | None = None
) -> ReplyDraftListResponse:
    """One profile's reply cards, newest first, optionally one status."""
    workspace_id = _require_workspace(ctx)
    doc = await _profile_doc(workspace_id, profile_id)
    if doc is None:
        return ReplyDraftListResponse(items=[])
    reply_query: dict[str, Any] = {"workspace": workspace_id, "profile": str(doc.id)}
    if status is not None:
        reply_query["status"] = status
    docs = (
        await _ReplyDoc.find(reply_query)
        .sort([("createdAt", -1), ("_id", -1)])
        .limit(REPLY_LIST_LIMIT)
        .to_list()
    )
    return ReplyDraftListResponse(items=[_reply_to_response(_reply_to_domain(d)) for d in docs])


async def update_reply(
    ctx: RequestContext, reply_id: str, body: UpdateReplyRequest
) -> ReplyDraftResponse:
    """Edit a card's text (re-checked, intent link rebuilt; 422 with the reasons when it
    breaks the rules) and/or set its status. A malformed or foreign id is a 404."""
    from pocketpaw_ee.cloud.growth.social import replies as social_replies

    body = UpdateReplyRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    try:
        oid = PydanticObjectId(reply_id)
    except Exception as exc:  # noqa: BLE001
        raise NotFound("social_reply", reply_id) from exc
    doc = await _ReplyDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("social_reply", reply_id)
    if body.draft_text is not None:
        reasons = social_replies.check_reply(body.draft_text, doc.source_text or doc.context)
        if reasons:
            raise ValidationError(
                "social.reply_rejected",
                "That reply breaks the drafting rules: " + "; ".join(reasons),
                {"reasons": reasons},
            )
        doc.draft_text = body.draft_text
        doc.intent_url = social_replies.intent_url(doc.target_post_id, body.draft_text)
    if body.status is not None:
        doc.status = body.status
    await doc.save()
    # no-event: Growth › Social has no realtime subscriber; the reply list re-fetches.
    return _reply_to_response(_reply_to_domain(doc))


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


# ---------------------------------------------------------------------------
# Accounts (Postiz)
# ---------------------------------------------------------------------------


TELEGRAM_CODE_TTL = timedelta(minutes=55)
_TELEGRAM_CONNECT = re.compile(r"^/connect(?:@\w+)?\s+(\S+)\s*$")


def _enabled_platforms() -> set[str]:
    from pocketpaw.config import get_settings

    raw = get_settings().postiz_platforms or ""
    chosen = {p.strip() for p in raw.split(",") if p.strip()}
    return chosen or {i for i, _ in SOCIAL_ACCOUNT_PLATFORMS}


def _account_platforms() -> list[SocialAccountPlatformResponse]:
    enabled = _enabled_platforms()
    return [
        SocialAccountPlatformResponse(id=i, label=label, available=i in enabled)
        for i, label in SOCIAL_ACCOUNT_PLATFORMS
    ]


def _account_response(raw: dict[str, Any]) -> SocialAccountResponse | None:
    account_id = str(raw.get("id") or "")
    if not account_id:
        return None
    username = raw.get("profile")
    picture = raw.get("picture")
    platform = str(raw.get("identifier") or "")
    return SocialAccountResponse(
        id=account_id,
        platform=platform,
        name=str(raw.get("name") or ""),
        username=username if isinstance(username, str) and username else None,
        picture=picture if isinstance(picture, str) and picture else None,
        disabled=bool(raw.get("disabled")),
        can_post=platform in POSTABLE_PLATFORMS,
        needs_channel=platform in CHANNEL_PLATFORMS,
    )


def _require_postiz() -> PostizClient:
    postiz = resolve_postiz()
    if postiz is None:
        raise CloudError(
            503,
            "social.accounts_unavailable",
            "Social account connections aren't set up on this deployment.",
        )
    return postiz


def _upstream(exc: PostizError) -> CloudError:
    logger.warning("social accounts: postiz failed (%s): %s", exc.code, exc.message)
    return CloudError(
        502, "social.accounts_upstream", "The social account service didn't respond. Try again."
    )


def _require_encryption() -> None:
    """Fail before Postiz makes an org whose key could not be stored."""
    encrypt("")


async def _postiz_org(workspace_id: str, profile: str) -> _PostizOrgDoc | None:
    return await _PostizOrgDoc.find_one({"workspace": workspace_id, "profile": profile})


async def _ensure_postiz_org(
    postiz: PostizClient, workspace_id: str, profile: _ProfileDoc
) -> _PostizOrgDoc:
    profile_key = str(profile.id)
    org = await _postiz_org(workspace_id, profile_key)
    if org is not None:
        return org
    _require_encryption()
    try:
        org_id, api_key = await postiz.create_org(
            f"{workspace_id}:{profile_key}", profile.company_name or "PocketPaw"
        )
    except PostizError as exc:
        raise _upstream(exc) from None
    doc = _PostizOrgDoc(
        workspace=workspace_id,
        profile=profile_key,
        postiz_org_id=org_id,
        api_key_enc=encrypt(api_key),
    )
    try:
        await doc.insert()
    except DuplicateKeyError:
        winner = await _postiz_org(workspace_id, profile_key)
        if winner is None:
            raise
        return winner
    # no-event: Growth › Social has no realtime subscriber; the Accounts page re-fetches.
    return doc


async def _integrations(postiz: PostizClient, org: _PostizOrgDoc) -> list[dict[str, Any]]:
    try:
        return await postiz.list_integrations(decrypt(org.api_key_enc))
    except PostizError as exc:
        raise _upstream(exc) from None


async def list_accounts(
    ctx: RequestContext, profile_id: str | None = None
) -> SocialAccountListResponse:
    workspace_id = _require_workspace(ctx)
    profile = await _require_profile_doc(workspace_id, profile_id)
    platforms = _account_platforms()
    postiz = resolve_postiz()
    if postiz is None:
        return SocialAccountListResponse(items=[], platforms=platforms, configured=False)
    org = await _postiz_org(workspace_id, str(profile.id))
    if org is None:
        return SocialAccountListResponse(items=[], platforms=platforms, configured=True)
    items = [a for a in map(_account_response, await _integrations(postiz, org)) if a]
    return SocialAccountListResponse(items=items, platforms=platforms, configured=True)


async def connect_account(
    ctx: RequestContext,
    profile_id: str | None,
    body: ConnectSocialAccountRequest,
    origin: str,
) -> ConnectSocialAccountResponse:
    body = ConnectSocialAccountRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    label = dict(SOCIAL_ACCOUNT_PLATFORMS).get(body.platform)
    if label is None:
        raise ValidationError(
            "social.platform_not_supported", f"Connecting {body.platform!r} isn't supported."
        )
    postiz = _require_postiz()
    if body.platform not in _enabled_platforms():
        raise ValidationError(
            "social.platform_unavailable", f"{label} isn't set up on this deployment yet."
        )
    bot_name = ""
    if body.platform == "telegram":
        _, bot_name = _telegram_bot()
    profile = await _require_profile_doc(workspace_id, profile_id)
    org = await _ensure_postiz_org(postiz, workspace_id, profile)
    redirect = f"{origin.rstrip('/')}/growth/social/accounts"
    try:
        if body.platform == "telegram":
            code = await postiz.connect_state(decrypt(org.api_key_enc), body.platform, redirect)
        else:
            url = await postiz.connect_url(decrypt(org.api_key_enc), body.platform, redirect)
    except PostizPlatformUnavailable:
        raise ValidationError(
            "social.platform_unavailable", f"{label} isn't set up on this deployment yet."
        ) from None
    except PostizError as exc:
        raise _upstream(exc) from None
    if body.platform != "telegram":
        return ConnectSocialAccountResponse(url=url)
    await org.set(
        {
            "telegram_code": code,
            "telegram_code_expires_at": datetime.now(UTC) + TELEGRAM_CODE_TTL,
            "telegram_chat_id": None,
        }
    )
    # no-event: Growth › Social has no realtime subscriber; the Accounts page polls the check.
    return ConnectSocialAccountResponse(
        url=None,
        telegram=TelegramConnectCode(
            code=code, bot_name=bot_name, expires_at=iso_utc(org.telegram_code_expires_at) or ""
        ),
    )


def _telegram_bot() -> tuple[str, str]:
    from pocketpaw.config import get_settings

    settings = get_settings()
    token = (settings.postiz_telegram_bot_token or "").strip()
    name = (settings.postiz_telegram_bot_name or "").strip().lstrip("@")
    if not (token and name):
        raise CloudError(
            503, "social.telegram_unavailable", "Telegram isn't set up on this deployment yet."
        )
    return token, name


async def _pending_telegram(workspace_id: str, profile: str, code: str) -> _PostizOrgDoc | None:
    return await _PostizOrgDoc.find_one(
        {
            "workspace": workspace_id,
            "profile": profile,
            "telegram_code": code,
            "telegram_code_expires_at": {"$gt": datetime.now(UTC)},
        }
    )


def _telegram_connects(updates: list[dict[str, Any]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post")
        if not isinstance(message, dict):
            continue
        match = _TELEGRAM_CONNECT.match(str(message.get("text") or ""))
        chat = message.get("chat")
        if match and isinstance(chat, dict) and chat.get("id") is not None:
            found[match.group(1)] = str(chat["id"])
    return found


async def _sweep_telegram(token: str) -> None:
    fetch = resolve_telegram_updates()
    try:
        updates = await fetch(token)
    except PostizError as exc:
        raise _upstream(exc) from None
    now = datetime.now(UTC)
    collection = _PostizOrgDoc.get_pymongo_collection()
    for code, chat_id in _telegram_connects(updates).items():
        # global-read: telegram sweep records every pending code so confirming
        # updates can't drop another tenant's
        await collection.update_many(
            {"telegram_code": code, "telegram_code_expires_at": {"$gt": now}},
            {"$set": {"telegram_chat_id": chat_id}},
        )
    # no-event: Growth › Social has no realtime subscriber; the Accounts page polls the check.
    ids = [u["update_id"] for u in updates if isinstance(u.get("update_id"), int)]
    if not ids:
        return
    try:
        await fetch(token, offset=max(ids) + 1)
    except PostizError as exc:
        logger.warning("social accounts: telegram confirm failed (%s): %s", exc.code, exc.message)


async def check_telegram(
    ctx: RequestContext, profile_id: str | None, body: CheckTelegramRequest
) -> CheckTelegramResponse:
    body = CheckTelegramRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    profile = await _require_profile_doc(workspace_id, profile_id)
    profile_key = str(profile.id)
    org = await _pending_telegram(workspace_id, profile_key, body.code)
    if org is None:
        raise NotFound("telegram_code", body.code)
    token, _ = _telegram_bot()
    postiz = _require_postiz()
    if not org.telegram_chat_id:
        await _sweep_telegram(token)
        org = await _pending_telegram(workspace_id, profile_key, body.code)
        if org is None:
            raise NotFound("telegram_code", body.code)
    if not org.telegram_chat_id:
        return CheckTelegramResponse(status="pending", account=None)
    try:
        raw = await postiz.complete_connect("telegram", body.code, org.telegram_chat_id)
    except PostizError as exc:
        raise _upstream(exc) from None
    await org.set(
        {"telegram_code": None, "telegram_code_expires_at": None, "telegram_chat_id": None}
    )
    # no-event: Growth › Social has no realtime subscriber; the Accounts page re-fetches.
    account = _account_response(
        {**raw, "identifier": raw.get("identifier") or raw.get("providerIdentifier")}
    )
    return CheckTelegramResponse(status="connected", account=account)


async def disconnect_account(ctx: RequestContext, profile_id: str | None, account_id: str) -> None:
    workspace_id = _require_workspace(ctx)
    profile = await _require_profile_doc(workspace_id, profile_id)
    postiz = resolve_postiz()
    org = await _postiz_org(workspace_id, str(profile.id)) if postiz else None
    if postiz is None or org is None:
        raise NotFound("social_account", account_id)
    if not any(str(i.get("id")) == account_id for i in await _integrations(postiz, org)):
        raise NotFound("social_account", account_id)
    try:
        await postiz.delete_integration(decrypt(org.api_key_enc), account_id)
    except PostizError as exc:
        raise _upstream(exc) from None
    # no-event: Growth › Social has no realtime subscriber; the Accounts page re-fetches.


async def _owned_accounts(
    workspace_id: str, profile: _ProfileDoc, postiz: PostizClient, account_id: str
) -> tuple[_PostizOrgDoc, dict[str, dict[str, Any]]]:
    org = await _postiz_org(workspace_id, str(profile.id))
    if org is None:
        raise NotFound("social_account", account_id)
    return org, {str(i.get("id")): i for i in await _integrations(postiz, org)}


def _platform_label(platform: str) -> str:
    return dict(SOCIAL_ACCOUNT_PLATFORMS).get(platform, platform)


async def list_channels(
    ctx: RequestContext, profile_id: str | None, account_id: str
) -> SocialChannelListResponse:
    workspace_id = _require_workspace(ctx)
    postiz = _require_postiz()
    profile = await _require_profile_doc(workspace_id, profile_id)
    org, owned = await _owned_accounts(workspace_id, profile, postiz, account_id)
    integration = owned.get(account_id)
    if integration is None:
        raise NotFound("social_account", account_id)
    platform = str(integration.get("identifier") or "")
    if platform not in CHANNEL_PLATFORMS:
        raise ValidationError(
            "social.channels_unsupported", f"{_platform_label(platform)} has no channels to pick."
        )
    try:
        output = await postiz.trigger_tool(decrypt(org.api_key_enc), account_id, "channels")
    except PostizError as exc:
        raise _upstream(exc) from None
    items = [
        SocialChannelResponse(id=str(c["id"]), name=str(c.get("name") or c["id"]))
        for c in (output if isinstance(output, list) else [])
        if isinstance(c, dict) and c.get("id")
    ]
    return SocialChannelListResponse(items=items)


MEDIA_NOT_PUBLIC = "Media must be at a public http(s) address. Upload it here first."
POST_SETTINGS: dict[str, dict[str, Any]] = {"x": {"who_can_reply_post": "everyone"}}


def _media_not_public() -> ValidationError:
    return ValidationError("social.media_not_public", MEDIA_NOT_PUBLIC)


def _media_storage_unavailable() -> CloudError:
    return CloudError(
        503,
        "social.media_storage_unavailable",
        "Media posting needs a public bucket (S3_PUBLIC_BUCKET).",
    )


async def upload_media(
    ctx: RequestContext, profile_id: str | None, data: bytes
) -> SocialMediaResponse:
    from pocketpaw.uploads.factory import build_public_adapter

    workspace_id = _require_workspace(ctx)
    profile = await _require_profile_doc(workspace_id, profile_id)
    mime = sniff_media_mime(data[:512])
    if mime is None or mime not in MEDIA_EXTENSIONS:
        raise ValidationError(
            "social.media_unsupported_type",
            "Upload a PNG, JPEG, WebP or GIF image, or an MP4 video.",
        )
    cap = max_bytes_for_mime(mime)
    if len(data) > cap:
        raise ValidationError(
            "social.media_too_large",
            f"{kind_for_mime(mime).capitalize()}s are capped at {cap // (1024 * 1024)} MB.",
        )
    adapter = build_public_adapter()
    if adapter is None:
        raise _media_storage_unavailable()
    digest = hashlib.sha256(data).hexdigest()
    key = f"growth-social/{workspace_id}/{profile.id}/{digest}.{MEDIA_EXTENSIONS[mime]}"
    url = adapter.public_url(key)
    if not url or not is_public_media_url(url):
        raise _media_storage_unavailable()

    async def _body() -> AsyncIterator[bytes]:
        yield data

    await adapter.put(key, _body(), mime)
    return SocialMediaResponse(id=digest, path=url, kind=kind_for_mime(mime))


def _check_post_media(body: CreateSocialPostRequest) -> bool:
    """Refuse private or mixed media; True when the post carries a video."""
    for item in body.media:
        if not is_public_media_url(item.path):
            raise _media_not_public()
    kinds = [media_kind(m.path) for m in body.media]
    if "video" in kinds and len(kinds) > 1:
        raise ValidationError(
            "social.media_mixed", "Attach one video on its own, or up to 4 images."
        )
    return "video" in kinds


LINK_ROUTE = "/api/v1/go"
_LINE_SPACES = re.compile(r"[ \t]+")


def link_base_url() -> str:
    """``POCKETPAW_GROWTH_LINK_BASE_URL``, else the public base + the mounted route."""
    from pocketpaw.config import get_settings

    base = (get_settings().growth_link_base_url or "").strip().rstrip("/")
    return base or f"{public_base_url()}{LINK_ROUTE}"


async def create_tracked_link(
    workspace_id: str, profile: str, idea: str, target_url: str
) -> _LinkDoc:
    """The idea's tracked link for ``target_url``, reused when it already exists."""
    if not is_web_url(target_url):
        raise ValidationError("social.link_url_invalid", "Only http(s) links can be tracked.")
    found = await _LinkDoc.find_one(
        {"workspace": workspace_id, "idea": idea, "target_url": target_url}
    )
    if found is not None:
        return found
    for _ in range(5):
        try:
            # no-event: Growth › Social has no realtime subscriber; links are read with ideas.
            return await _LinkDoc(
                code=new_code(),
                workspace=workspace_id,
                profile=profile,
                idea=idea,
                target_url=target_url,
            ).insert()
        except DuplicateKeyError:
            continue
    raise ConflictError("social.link_code_taken", "Could not mint a short link, try again.")


async def record_click(
    code: str, user_agent: str | None, now: datetime | None = None
) -> str | None:
    """The stored target for ``code`` (None when unknown). Counts the click unless a
    preview bot made it. Nothing about the requester is stored."""
    if not valid_code(code):
        return None
    # global-read: the public short code is the only key a click carries; it names one link.
    doc = await _LinkDoc.find_one({"code": code})
    if doc is None:
        return None
    if not is_preview_bot(user_agent):
        day = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%d")
        # no-event: a click counter; nothing subscribes.
        await _LinkDoc.find_one({"_id": doc.id}).update(
            {"$inc": {"clicks": 1, f"clicks_by_day.{day}": 1}}
        )
    return doc.target_url


async def link_clicks(workspace_id: str, idea_ids: list[str]) -> dict[str, int]:
    """Total tracked-link clicks per idea, within ``workspace_id`` only."""
    totals: dict[str, int] = {}
    for link in await _LinkDoc.find(
        {"workspace": workspace_id, "idea": {"$in": idea_ids}}
    ).to_list():
        totals[link.idea] = totals.get(link.idea, 0) + link.clicks
    return totals


async def _track_first_link(
    workspace_id: str, profile: str, idea: str, content: str, parts: list[str]
) -> tuple[str, list[str]]:
    """Swap the post's first URL for a tracked link posted as a reply part."""
    urls = extract_urls(content)
    if not urls:
        return content, parts
    link = await create_tracked_link(workspace_id, profile, idea, urls[0])
    tracked = f"{link_base_url()}/{link.code}"
    lines = content.replace(urls[0], "", 1).split("\n")
    text = "\n".join(_LINE_SPACES.sub(" ", line).strip() for line in lines).strip()
    if not text:
        # The post was only the link: keep it in the body, tracked.
        return tracked, parts
    return text, [*parts, f"Link: {tracked}"]


async def create_post(
    ctx: RequestContext, profile_id: str | None, body: CreateSocialPostRequest
) -> CreateSocialPostResponse:
    body = CreateSocialPostRequest.model_validate(body)
    workspace_id = _require_workspace(ctx)
    has_video = _check_post_media(body)
    postiz = _require_postiz()
    profile = await _require_profile_doc(workspace_id, profile_id)
    idea: _IdeaDoc | None = None
    if body.idea_id is not None:
        idea = await _fetch_idea_in_workspace(workspace_id, body.idea_id)
        if idea.profile != str(profile.id):
            raise NotFound("social_idea", body.idea_id)
    org, owned = await _owned_accounts(workspace_id, profile, postiz, body.targets[0].account_id)
    platforms: dict[str, str] = {}
    for target in body.targets:
        if target.account_id not in owned:
            raise NotFound("social_account", target.account_id)
        platform = str(owned[target.account_id].get("identifier") or "")
        if platform not in POSTABLE_PLATFORMS:
            raise ValidationError(
                "social.post_platform_unsupported",
                f"Posting to {_platform_label(platform)} isn't supported yet.",
            )
        if platform in CHANNEL_PLATFORMS and not target.channel:
            raise ValidationError(
                "social.channel_required",
                f"Pick a channel to post to in {_platform_label(platform)}.",
            )
        platforms[target.account_id] = platform
    content, parts = body.content, list(body.parts)
    if idea is not None:
        content, parts = await _track_first_link(
            workspace_id, str(profile.id), str(idea.id), content, parts
        )
    media = [{"id": m.id, "path": m.path} for m in body.media]
    thread = [{"content": part, "image": []} for part in parts]
    skipped = {a for a, p in platforms.items() if has_video and p not in VIDEO_PLATFORMS}
    posts = []
    for target in body.targets:
        platform = platforms[target.account_id]
        settings: dict[str, Any] = {"__type": platform, **POST_SETTINGS.get(platform, {})}
        if target.channel:
            settings["channel"] = target.channel
        first = {"content": content, "image": [] if target.account_id in skipped else media}
        posts.append(
            {
                "integration": {"id": target.account_id},
                "value": [first, *thread],
                "settings": settings,
            }
        )
    when = body.scheduled_at or datetime.now(UTC)
    payload = {
        "type": "now" if body.scheduled_at is None else "schedule",
        "date": when.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "shortLink": False,
        "tags": [],
        "posts": posts,
    }
    try:
        created = await postiz.create_posts(decrypt(org.api_key_enc), payload)
    except PostizError as exc:
        raise _upstream(exc) from None
    # no-event: Growth › Social has no realtime subscriber; the composer polls the posts list.
    post_ids = {str(c.get("integration")): str(c["postId"]) for c in created if c.get("postId")}
    items = [
        SocialPostCreated(
            account_id=a, platform=platforms[a], post_id=post_ids[a], media_skipped=a in skipped
        )
        for a in platforms
        if a in post_ids
    ]
    if not items:
        raise _upstream(PostizError("postiz.bad_response", "Postiz created no post."))
    if idea is not None:
        # no-event: Growth › Social has no realtime subscriber; Blitz re-fetches.
        await _IdeaDoc.find_one({"_id": idea.id, "workspace": workspace_id}).update(
            {"$addToSet": {"post_ids": {"$each": [i.post_id for i in items]}}}
        )
    return CreateSocialPostResponse(items=items)


POST_STATES = {"QUEUE": "queued", "PUBLISHED": "published", "ERROR": "error", "DRAFT": "draft"}
POSTS_LOOKBACK = timedelta(days=7)
POSTS_LOOKAHEAD = timedelta(days=1)


def _post_response(raw: dict[str, Any]) -> SocialPostResponse | None:
    post_id = str(raw.get("id") or "")
    if not post_id or raw.get("deletedAt"):
        return None
    integration = raw.get("integration") if isinstance(raw.get("integration"), dict) else {}
    raw_state = str(raw.get("state") or "")
    state = POST_STATES.get(raw_state, raw_state.lower())
    url, error, published = raw.get("releaseURL"), raw.get("error"), raw.get("publishDate")
    return SocialPostResponse(
        id=post_id,
        account_id=str(integration.get("id") or ""),
        platform=str(integration.get("providerIdentifier") or ""),
        account_name=str(integration.get("name") or ""),
        content=str(raw.get("content") or ""),
        state=state,
        url=url if isinstance(url, str) and url else None,
        error=error if isinstance(error, str) and error else None,
        published_at=published if state == "published" and isinstance(published, str) else None,
    )


async def list_posts(
    ctx: RequestContext, profile_id: str | None = None, ids: str | None = None
) -> SocialPostListResponse:
    workspace_id = _require_workspace(ctx)
    postiz = _require_postiz()
    profile = await _require_profile_doc(workspace_id, profile_id)
    org = await _postiz_org(workspace_id, str(profile.id))
    if org is None:
        return SocialPostListResponse(items=[])
    now = datetime.now(UTC)
    try:
        raw = await postiz.list_posts(
            decrypt(org.api_key_enc),
            (now - POSTS_LOOKBACK).isoformat(),
            (now + POSTS_LOOKAHEAD).isoformat(),
        )
    except PostizError as exc:
        raise _upstream(exc) from None
    wanted = {i.strip() for i in (ids or "").split(",") if i.strip()} or None
    raw = sorted(raw, key=lambda p: str(p.get("publishDate") or ""), reverse=True)
    items = [p for p in map(_post_response, raw) if p and (wanted is None or p.id in wanted)]
    return SocialPostListResponse(items=items)


# ---------------------------------------------------------------------------
# Post metrics (published record + X metric snapshots)
# ---------------------------------------------------------------------------

PUBLISHED_LOOKBACK = timedelta(days=30)
METRIC_LATE_DAYS = 3
_X_STATUS_URL = re.compile(r"^https?://(?:[\w-]+\.)?(?:x|twitter)\.com/(?:[^/?#]+/)*status/(\d+)")


def x_status_id(url: Any) -> str:
    """The numeric post id in an x.com / twitter.com ``.../status/<id>`` URL, else ''."""
    m = _X_STATUS_URL.match(url) if isinstance(url, str) else None
    return m.group(1) if m else ""


def _parse_iso(value: Any, default: datetime) -> datetime:
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return default


def due_bucket(published_at: datetime, now: datetime) -> int | None:
    """The one age bucket a snapshot taken ``now`` belongs to: the largest bucket the post
    has reached. Smaller missed buckets are never back-filled (their numbers would be
    today's, not that age's), so each bucket's snapshot is taken in its own window."""
    age = now - _aware(published_at)
    reached = [b for b in METRIC_AGE_BUCKETS if age >= timedelta(days=b)]
    return reached[-1] if reached else None


async def _fill_published(
    postiz: PostizClient, workspace_id: str, profile: str, docs: list[_IdeaDoc], now: datetime
) -> int:
    """Record the live X post of each idea Postiz reports as published. Returns how many."""
    org = await _postiz_org(workspace_id, profile)
    if org is None:
        return 0
    raw = await postiz.list_posts(
        decrypt(org.api_key_enc),
        (now - PUBLISHED_LOOKBACK).isoformat(),
        (now + POSTS_LOOKAHEAD).isoformat(),
    )
    posts = {str(p.get("id")): p for p in raw if p.get("id")}
    filled = 0
    for doc in docs:
        for pid in doc.post_ids:
            post = posts.get(pid) or {}
            x_id = x_status_id(post.get("releaseURL"))
            if post.get("state") != "PUBLISHED" or not x_id:
                continue
            # no-event: Growth › Social has no realtime subscriber; Blitz re-fetches.
            await _IdeaDoc.find_one({"_id": doc.id, "workspace": workspace_id}).update(
                {
                    "$set": {
                        "published_url": post["releaseURL"],
                        "published_at": _parse_iso(post.get("publishDate"), now),
                        "x_post_id": x_id,
                    }
                }
            )
            filled += 1
            break
    return filled


async def _snapshot_workspace(
    metrics_fn: Any, workspace_id: str, docs: list[_IdeaDoc], now: datetime
) -> tuple[int, int]:
    """Store each idea's due bucket that is not stored yet. Returns (due, stored)."""
    ids = [str(d.id) for d in docs]
    stored = {
        (m.idea, m.age_bucket)
        for m in await _MetricDoc.find({"workspace": workspace_id, "idea": {"$in": ids}}).to_list()
    }
    due: list[tuple[_IdeaDoc, int]] = []
    for doc in docs:
        bucket = due_bucket(doc.published_at, now) if doc.published_at else None
        if bucket is not None and (str(doc.id), bucket) not in stored:
            due.append((doc, bucket))
    if not due:
        return 0, 0
    read = await metrics_fn(workspace_id, sorted({d.x_post_id for d, _ in due}))
    saved = 0
    for doc, bucket in due:
        counts = read.get(doc.x_post_id)
        if not counts:
            continue  # unread (cap, error, deleted post): never store zeros for it
        try:
            await _MetricDoc(
                workspace=workspace_id,
                profile=doc.profile,
                idea=str(doc.id),
                x_post_id=doc.x_post_id,
                age_bucket=bucket,
                captured_at=now,
                **{k: int(counts.get(k, 0)) for k in METRIC_FIELDS},
            ).insert()
            saved += 1
        except DuplicateKeyError:
            pass  # another run stored this bucket first; snapshots are never overwritten
    # no-event: an append-only metrics log; nothing listens, the Today strip re-fetches.
    return len(due), saved


async def run_post_metrics(now: datetime | None = None) -> dict[str, int]:
    """Record what got published and snapshot its X metrics, for every workspace.

    Step A: X ideas with Postiz ``post_ids`` and no ``x_post_id`` (created in the last
    ``PUBLISHED_LOOKBACK``) get ``published_url`` / ``published_at`` / ``x_post_id`` from
    the published Postiz post, when Postiz is configured. Step B: X ideas published within
    the last 28 + ``METRIC_LATE_DAYS`` days get one snapshot of their ``due_bucket`` when
    that bucket is not stored yet, read in one batched call per workspace. A failing
    profile or workspace never stops the rest. Read-only towards X. Runs from the growth
    worker's 07:00 UTC cron."""
    from pocketpaw_ee.cloud.growth.social import metrics as social_metrics

    now = now or datetime.now(UTC)
    counts = {"filled": 0, "workspaces": 0, "due": 0, "stored": 0, "failed": 0}
    metrics_fn = social_metrics.resolve_metrics_fn()
    if metrics_fn is None:
        return counts

    postiz = resolve_postiz()
    if postiz is not None:
        # global-read: the sweep runs for every workspace; each doc carries its own tenant.
        pending = await _IdeaDoc.find(
            {
                "platform": "x",
                "post_ids.0": {"$exists": True},
                "x_post_id": {"$in": ["", None]},
                "createdAt": {"$gte": now - PUBLISHED_LOOKBACK},
            }
        ).to_list()
        groups: dict[tuple[str, str], list[_IdeaDoc]] = {}
        for doc in pending:
            groups.setdefault((doc.workspace, doc.profile), []).append(doc)
        for (workspace_id, profile), docs in groups.items():
            try:
                counts["filled"] += await _fill_published(postiz, workspace_id, profile, docs, now)
            except Exception:  # noqa: BLE001
                counts["failed"] += 1
                logger.warning("post metrics: lookup %s failed", workspace_id, exc_info=True)

    # global-read: the sweep runs for every workspace; each doc carries its own tenant.
    published = await _IdeaDoc.find(
        {
            "platform": "x",
            "x_post_id": {"$gt": ""},
            "published_at": {
                "$gte": now - timedelta(days=METRIC_AGE_BUCKETS[-1] + METRIC_LATE_DAYS)
            },
        }
    ).to_list()
    by_workspace: dict[str, list[_IdeaDoc]] = {}
    for doc in published:
        by_workspace.setdefault(doc.workspace, []).append(doc)
    counts["workspaces"] = len(by_workspace)
    for workspace_id, docs in by_workspace.items():
        try:
            due, saved = await _snapshot_workspace(metrics_fn, workspace_id, docs, now)
            counts["due"] += due
            counts["stored"] += saved
        except Exception:  # noqa: BLE001
            counts["failed"] += 1
            logger.warning("post metrics: workspace=%s failed", workspace_id, exc_info=True)
    logger.info(
        "post metrics: filled=%d workspaces=%d due=%d stored=%d failed=%d",
        counts["filled"],
        counts["workspaces"],
        counts["due"],
        counts["stored"],
        counts["failed"],
    )
    return counts


async def latest_metrics(workspace_id: str, idea_ids: list[str]) -> dict[str, PostMetrics]:
    """The most recently captured snapshot of each idea, within ``workspace_id`` only."""
    latest: dict[str, _MetricDoc] = {}
    rows = await _MetricDoc.find({"workspace": workspace_id, "idea": {"$in": idea_ids}}).to_list()
    for m in rows:
        if m.idea not in latest or _aware(m.captured_at) > _aware(latest[m.idea].captured_at):
            latest[m.idea] = m
    return {
        idea: PostMetrics(
            workspace_id=m.workspace,
            idea_id=m.idea,
            x_post_id=m.x_post_id,
            age_bucket=m.age_bucket,
            captured_at=m.captured_at,
            **{k: getattr(m, k) for k in METRIC_FIELDS},
        )
        for idea, m in latest.items()
    }


__all__ = [
    "create_tracked_link",
    "link_base_url",
    "link_clicks",
    "record_click",
    "latest_metrics",
    "run_post_metrics",
    "check_telegram",
    "connect_account",
    "create_post",
    "upload_media",
    "list_channels",
    "list_posts",
    "disconnect_account",
    "list_accounts",
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
    "draft_daily",
    "draft_daily_for_profile",
    "run_daily_drafts",
    "run_repo_metrics",
    "repo_metrics",
    "metrics_summary",
    "get_profile",
    "list_ideas",
    "update_idea",
    "upsert_profile",
]
