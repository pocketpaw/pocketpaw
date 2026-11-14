# ee/pocketpaw_ee/cloud/growth/social/dto.py — request/response DTOs for
# Growth › Social (``/growth/social/...``). Field names and enum values are the
# shared API contract in docs/design/drafts/2026-10-06-growth-social-setup.md;
# the paw-enterprise client is built against them.
#
# Boundary rules enforced here: ``UpsertProfileRequest`` is a partial upsert —
# an omitted field is left alone, an explicit ``null`` clears a nullable field
# (the service reads ``model_fields_set``), and ``description`` merges key by
# key so wizard steps can write it piecemeal; ``analysis`` (a Brand-page hand
# edit) replaces only the editable fields sent, never ``pages_read`` /
# ``logo_url``, with lists capped at 20 items of 300 characters and strings
# at 2000. ``website`` is trimmed and gets ``https://`` when typed bare;
# anything that is not then an http(s) URL with a host is a 422. ``category``
# is free text up to 60 characters. ``active`` / ``weekly_post_quota`` (0..21) /
# ``github_repos`` (at most 5 ``owner/name`` slugs, trimmed and deduped in
# order) reset to their defaults on an explicit ``null``; so do the reply-card
# settings: ``reply_watch_handles`` (at most 30 X handles, leading ``@`` dropped,
# deduped ignoring case), ``reply_queries`` (at most 5, each trimmed and at most
# 200 characters) and ``reply_cards_per_day`` (1..25, default 15). A reply edit
# needs ``draft_text`` (at most 280 characters) and/or ``status``. Idea edits need at
# least one field; script beats and hashtags are trimmed and capped. A post
# names 1–10 distinct accounts and trimmed, non-empty content of at most 10000
# characters, plus up to 25 thread parts held to the same rule and up to 4
# media items; whether the media is public and unmixed is the service's check,
# so it carries an error code. A post's optional ``scheduled_at`` is read as UTC
# when it has no offset, normalised to UTC, and must not be more than
# ``SCHEDULE_SKEW`` in the past.

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from pocketpaw_ee.cloud.growth.social.domain import (
    DEFAULT_IDEA_COUNT,
    MAX_IDEA_COUNT,
    MAX_POST_IMAGES,
    MAX_REPLY_CARDS_PER_DAY,
    MAX_REPLY_CHARS,
    MAX_REPLY_QUERIES,
    MAX_REPLY_QUERY_CHARS,
    MAX_REPLY_WATCH_HANDLES,
    AnalysisStatus,
    BusinessModel,
    IdeaFormat,
    IdeaSource,
    IdeaStatus,
    MonthlyRevenue,
    Platform,
    ReplyStatus,
    SocialRole,
    TeamSize,
)

MAX_SCRIPT_BEATS = 12
MAX_HASHTAGS = 15
MAX_ANALYSIS_ITEMS = 20
MAX_GITHUB_REPOS = 5
MAX_WEEKLY_POST_QUOTA = 21
SCHEDULE_SKEW = timedelta(minutes=1)
_GITHUB_REPO = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
_X_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

_AnalysisItem = Annotated[str, StringConstraints(max_length=300)]
_AnalysisList = Annotated[list[_AnalysisItem], Field(max_length=MAX_ANALYSIS_ITEMS)]


def normalise_website(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        raise ValueError("website must be an http(s) address like example.com")
    return candidate


def _clean_list(values: list[str] | None, limit: int, item_max: int) -> list[str] | None:
    if values is None:
        return None
    out: list[str] = []
    for raw in values:
        text = raw.strip()
        if text:
            out.append(text[:item_max])
    return out[:limit]


class DescriptionPatch(BaseModel):
    product: str | None = Field(default=None, max_length=2000)
    audience: str | None = Field(default=None, max_length=2000)
    problem: str | None = Field(default=None, max_length=2000)
    benefits: str | None = Field(default=None, max_length=2000)
    tone: str | None = Field(default=None, max_length=2000)
    avoid: str | None = Field(default=None, max_length=2000)


class AnalysisPatch(BaseModel):
    """A hand edit of the analysis from the Brand page. Only the fields sent
    are replaced. ``pages_read`` and ``logo_url`` are server-owned and ignored
    if present (unknown keys are dropped)."""

    summary: str | None = Field(default=None, max_length=2000)
    product: str | None = Field(default=None, max_length=2000)
    audience: str | None = Field(default=None, max_length=2000)
    problem: str | None = Field(default=None, max_length=2000)
    tone: str | None = Field(default=None, max_length=2000)
    benefits: _AnalysisList | None = None
    differentiators: _AnalysisList | None = None
    competitors: _AnalysisList | None = None
    avoid: _AnalysisList | None = None
    content_pillars: _AnalysisList | None = None
    hooks: _AnalysisList | None = None

    @field_validator("summary", "product", "audience", "problem", "tone")
    @classmethod
    def _strip_text(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v

    @field_validator(
        "benefits", "differentiators", "competitors", "avoid", "content_pillars", "hooks"
    )
    @classmethod
    def _strip_items(cls, v: list[str] | None) -> list[str] | None:
        return _clean_list(v, MAX_ANALYSIS_ITEMS, 300)


class UpsertProfileRequest(BaseModel):
    """Partial upsert of the typed profile fields. Omitted means "leave as-is";
    an explicit ``null`` clears a nullable field (``owner_name`` /
    ``company_name`` clear to ``""``; ``active``, ``weekly_post_quota`` and
    ``github_repos`` reset to ``False`` / ``5`` / ``[]``)."""

    owner_name: str | None = Field(default=None, max_length=120)
    company_name: str | None = Field(default=None, max_length=120)
    website: str | None = Field(default=None, max_length=2048)
    description: DescriptionPatch | None = None
    team_size: TeamSize | None = None
    monthly_revenue: MonthlyRevenue | None = None
    role: SocialRole | None = None
    business_model: BusinessModel | None = None
    category: str | None = Field(default=None, max_length=60)
    analysis: AnalysisPatch | None = None
    active: bool | None = None
    weekly_post_quota: int | None = Field(default=None, ge=0, le=MAX_WEEKLY_POST_QUOTA)
    github_repos: list[str] | None = None
    reply_watch_handles: list[str] | None = None
    reply_queries: list[str] | None = None
    reply_cards_per_day: int | None = Field(default=None, ge=1, le=MAX_REPLY_CARDS_PER_DAY)

    @field_validator("github_repos")
    @classmethod
    def _repos(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out: list[str] = []
        for raw in v:
            slug = raw.strip()
            if not _GITHUB_REPO.match(slug):
                raise ValueError("github_repos items must look like owner/name")
            if slug not in out:
                out.append(slug)
        if len(out) > MAX_GITHUB_REPOS:
            raise ValueError(f"at most {MAX_GITHUB_REPOS} github_repos")
        return out

    @field_validator("reply_watch_handles")
    @classmethod
    def _handles(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out: list[str] = []
        for raw in v:
            handle = raw.strip().lstrip("@")
            if not _X_HANDLE.match(handle):
                raise ValueError("reply_watch_handles items must be X handles (A-Z, 0-9, _)")
            if handle.lower() not in {h.lower() for h in out}:
                out.append(handle)
        if len(out) > MAX_REPLY_WATCH_HANDLES:
            raise ValueError(f"at most {MAX_REPLY_WATCH_HANDLES} reply_watch_handles")
        return out

    @field_validator("reply_queries")
    @classmethod
    def _queries(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out: list[str] = []
        for raw in v:
            query = raw.strip()
            if len(query) > MAX_REPLY_QUERY_CHARS:
                raise ValueError(f"reply_queries items are at most {MAX_REPLY_QUERY_CHARS} chars")
            if query and query not in out:
                out.append(query)
        if len(out) > MAX_REPLY_QUERIES:
            raise ValueError(f"at most {MAX_REPLY_QUERIES} reply_queries")
        return out

    @field_validator("website")
    @classmethod
    def _website(cls, v: str | None) -> str | None:
        return normalise_website(v)

    @field_validator("owner_name", "company_name", "category")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) else v


class DescriptionResponse(BaseModel):
    product: str = ""
    audience: str = ""
    problem: str = ""
    benefits: str = ""
    tone: str = ""
    avoid: str = ""


class SocialAnalysisResponse(BaseModel):
    summary: str = ""
    product: str = ""
    audience: str = ""
    problem: str = ""
    tone: str = ""
    benefits: list[str] = Field(default_factory=list)
    differentiators: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    content_pillars: list[str] = Field(default_factory=list)
    hooks: list[str] = Field(default_factory=list)
    pages_read: list[str] = Field(default_factory=list)
    logo_url: str | None = None


class CharacterResponse(BaseModel):
    id: str
    name: str
    description: str
    svg: str


class SocialProfileResponse(BaseModel):
    id: str
    workspace_id: str
    owner_name: str
    company_name: str
    website: str | None
    description: DescriptionResponse
    team_size: TeamSize | None
    monthly_revenue: MonthlyRevenue | None
    role: SocialRole | None
    business_model: BusinessModel | None
    category: str | None
    analysis_status: AnalysisStatus
    analysis_error: str | None
    analysis: SocialAnalysisResponse | None
    analyzed_at: str | None
    onboarding_completed_at: str | None
    created_at: str | None
    updated_at: str | None
    characters: list[CharacterResponse] = Field(default_factory=list)
    active: bool
    weekly_post_quota: int
    github_repos: list[str]
    reply_watch_handles: list[str]
    reply_queries: list[str]
    reply_cards_per_day: int


class GenerateIdeasRequest(BaseModel):
    count: int = Field(default=DEFAULT_IDEA_COUNT, ge=1, le=MAX_IDEA_COUNT)
    platform: Platform | None = None


class UpdateIdeaRequest(BaseModel):
    """Review status and/or copy edits for one idea. ``format`` and ``why`` are
    the generator's and are not editable."""

    status: IdeaStatus | None = None
    hook: str | None = Field(default=None, min_length=1, max_length=300)
    on_screen_text: str | None = Field(default=None, max_length=500)
    caption: str | None = Field(default=None, max_length=2200)
    script: list[str] | None = Field(default=None, max_length=50)
    hashtags: list[str] | None = Field(default=None, max_length=50)
    poster_svg: str | None = Field(default=None, min_length=1, max_length=2_000_000)
    media_choice: Literal["poster", "reel"] | None = None

    @field_validator("hook")
    @classmethod
    def _non_blank_hook(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("hook must not be blank")
        return v.strip() if v is not None else v

    @field_validator("script")
    @classmethod
    def _script(cls, v: list[str] | None) -> list[str] | None:
        return _clean_list(v, MAX_SCRIPT_BEATS, 300)

    @field_validator("hashtags")
    @classmethod
    def _hashtags(cls, v: list[str] | None) -> list[str] | None:
        return _clean_list(v, MAX_HASHTAGS, 60)

    @model_validator(mode="after")
    def _at_least_one(self) -> UpdateIdeaRequest:
        if not self.model_fields_set or all(
            getattr(self, name) is None for name in self.model_fields_set
        ):
            raise ValueError(
                "provide at least one of status / hook / on_screen_text / caption / "
                "script / hashtags"
            )
        return self


class SocialIdeaResponse(BaseModel):
    id: str
    workspace_id: str
    format: IdeaFormat
    hook: str
    on_screen_text: str
    caption: str
    why: str
    script: list[str]
    hashtags: list[str]
    platform: Literal["", "x", "reddit"]
    subreddit: str
    status: IdeaStatus
    scheduled_at: str | None
    calendar_event_id: str
    poster_svg: str
    reel_html: str
    media_choice: Literal["", "poster", "reel"]
    source: IdeaSource
    suggested_at: str | None
    post_ids: list[str]
    published_url: str
    published_at: str | None
    x_post_id: str
    created_at: str | None
    updated_at: str | None


class SocialIdeaListResponse(BaseModel):
    items: list[SocialIdeaResponse]


class ReplyDraftResponse(BaseModel):
    id: str
    workspace_id: str
    profile_id: str
    platform: Literal["x"]
    target_post_id: str
    target_url: str
    author: str
    context: str
    draft_text: str
    intent_url: str
    why: str
    status: ReplyStatus
    created_at: str | None
    updated_at: str | None


class ReplyDraftListResponse(BaseModel):
    items: list[ReplyDraftResponse]


class UpdateReplyRequest(BaseModel):
    """Edit a reply card's text and/or record what was done with it."""

    draft_text: str | None = Field(default=None, min_length=1, max_length=MAX_REPLY_CHARS)
    status: ReplyStatus | None = None

    @field_validator("draft_text")
    @classmethod
    def _strip_text(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("draft_text must not be blank")
        return v.strip() if v is not None else v

    @model_validator(mode="after")
    def _at_least_one(self) -> UpdateReplyRequest:
        if self.draft_text is None and self.status is None:
            raise ValueError("provide draft_text and/or status")
        return self


class SocialProfileListResponse(BaseModel):
    items: list[SocialProfileResponse]


class ScheduleItem(BaseModel):
    idea_id: str = Field(min_length=1, max_length=64)
    scheduled_at: datetime


class ScheduleIdeasRequest(BaseModel):
    """Put approved ideas on the calendar. ``timezone`` is the IANA zone the
    calendar events are shown in; each event lasts ``duration_minutes``."""

    items: list[ScheduleItem] = Field(min_length=1, max_length=100)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    duration_minutes: int = Field(default=30, ge=5, le=240)


class MakeMediaRequest(BaseModel):
    kind: Literal["poster", "reel"]


class CreateCharacterRequest(BaseModel):
    name: str = Field(default="", max_length=40)
    description: str = Field(min_length=3, max_length=400)


class MakeMemeRequest(BaseModel):
    format: str | None = Field(default=None, max_length=40)
    character_id: str | None = Field(default=None, max_length=32)
    mention_business: bool = True
    prompt: str = Field(default="", max_length=500)
    platform: Platform = "x"


class MemeFormatResponse(BaseModel):
    id: str
    name: str
    layout: str


class MemeFormatListResponse(BaseModel):
    items: list[MemeFormatResponse]


class MetricsPostResponse(BaseModel):
    """One X post published yesterday, with its latest snapshot and tracked-link clicks."""

    idea_id: str
    hook: str
    published_url: str
    published_at: datetime
    impressions: int = 0
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    clicks: int = 0


class RepoStarsPointResponse(BaseModel):
    day: str
    stars: int


class MetricsRepoResponse(BaseModel):
    repo: str
    traffic_available: bool = False
    uniques_week: int = 0
    stars_week_delta: int = 0
    stars: list[RepoStarsPointResponse] = Field(default_factory=list)


class MetricsSummaryResponse(BaseModel):
    """The Today page's morning strip: yesterday's posts and the profile's repos."""

    posts: list[MetricsPostResponse] = Field(default_factory=list)
    repos: list[MetricsRepoResponse] = Field(default_factory=list)


class PulseSourceModel(BaseModel):
    platform: Literal["reddit", "x"]
    kind: Literal["subreddit", "search"]
    value: str = Field(min_length=2, max_length=120)


class RefreshPulseRequest(BaseModel):
    """Sources to read; omitted = keep the last ones, or let the agent pick from the profile."""

    sources: list[PulseSourceModel] | None = Field(default=None, max_length=6)


class PulseEvidenceResponse(BaseModel):
    id: str
    platform: Literal["reddit", "x"]
    url: str
    title: str
    text: str
    score: int
    source: str
    comments: list[str]
    new: bool = False


class PulseSuggestionResponse(BaseModel):
    platform: Literal["reddit", "x"]
    hook: str
    caption: str
    subreddit: str


class PulseCompanyResponse(BaseModel):
    name: str
    domain: str
    reason: str
    url: str


class PulseThemeResponse(BaseModel):
    title: str
    why: str
    sentiment: str
    phrases: list[str]
    evidence: list[PulseEvidenceResponse]
    suggestions: list[PulseSuggestionResponse]
    companies: list[PulseCompanyResponse] = Field(default_factory=list)


class SocialPulseResponse(BaseModel):
    sources: list[PulseSourceModel]
    themes: list[PulseThemeResponse]
    refreshed_at: str | None


class UsePulseSuggestionRequest(BaseModel):
    theme: int = Field(ge=0, le=10)
    suggestion: int = Field(ge=0, le=5)
    status: Literal["new", "approved"] = "new"


class AddPulseCompaniesRequest(BaseModel):
    theme: int = Field(ge=0, le=10)


class AddPulseCompaniesResponse(BaseModel):
    added: list[str]
    skipped: list[str]


class SocialAccountResponse(BaseModel):
    id: str
    platform: str
    name: str
    username: str | None
    picture: str | None
    disabled: bool
    can_post: bool
    needs_channel: bool


class SocialAccountPlatformResponse(BaseModel):
    id: str
    label: str
    available: bool


class SocialAccountListResponse(BaseModel):
    items: list[SocialAccountResponse]
    platforms: list[SocialAccountPlatformResponse]
    configured: bool


class ConnectSocialAccountRequest(BaseModel):
    platform: str = Field(max_length=64)


class TelegramConnectCode(BaseModel):
    code: str
    bot_name: str
    expires_at: str


class ConnectSocialAccountResponse(BaseModel):
    url: str | None
    telegram: TelegramConnectCode | None = None


class CheckTelegramRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class CheckTelegramResponse(BaseModel):
    status: Literal["pending", "connected"]
    account: SocialAccountResponse | None


class SocialChannelResponse(BaseModel):
    id: str
    name: str


class SocialChannelListResponse(BaseModel):
    items: list[SocialChannelResponse]


class SocialPostTarget(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)
    channel: str | None = Field(default=None, max_length=128)

    @field_validator("channel")
    @classmethod
    def _channel(cls, v: str | None) -> str | None:
        return (v or "").strip() or None


_PostContent = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10000)
]


class SocialPostMedia(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=2048)


class CreateSocialPostRequest(BaseModel):
    content: _PostContent
    targets: list[SocialPostTarget] = Field(min_length=1, max_length=10)
    parts: list[_PostContent] = Field(default_factory=list, max_length=25)
    media: list[SocialPostMedia] = Field(default_factory=list, max_length=MAX_POST_IMAGES)
    idea_id: str | None = Field(default=None, min_length=1, max_length=64)
    scheduled_at: datetime | None = None

    @field_validator("scheduled_at")
    @classmethod
    def _future(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        when = (v if v.tzinfo else v.replace(tzinfo=UTC)).astimezone(UTC)
        if when < datetime.now(UTC) - SCHEDULE_SKEW:
            raise ValueError("scheduled_at must be in the future")
        return when

    @model_validator(mode="after")
    def _unique_accounts(self) -> CreateSocialPostRequest:
        ids = [t.account_id for t in self.targets]
        if len(set(ids)) != len(ids):
            raise ValueError("each account may be targeted once")
        return self


class SocialPostCreated(BaseModel):
    account_id: str
    platform: str
    post_id: str
    media_skipped: bool = False


class CreateSocialPostResponse(BaseModel):
    items: list[SocialPostCreated]


class SocialMediaResponse(BaseModel):
    id: str
    path: str
    kind: Literal["image", "video"]


class SocialPostResponse(BaseModel):
    id: str
    account_id: str
    platform: str
    account_name: str
    content: str
    state: str
    url: str | None
    error: str | None
    published_at: str | None


class SocialPostListResponse(BaseModel):
    items: list[SocialPostResponse]
