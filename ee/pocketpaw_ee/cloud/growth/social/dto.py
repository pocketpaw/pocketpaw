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
# least one field; script beats and hashtags are trimmed and capped, and the
# ``pillar`` / ``hook_type`` tags are trimmed and cut to 40 characters. A post
# names 1–10 distinct accounts and trimmed, non-empty content of at most 10000
# characters, plus up to 25 thread parts held to the same rule and up to 4
# media items; whether the media is public and unmixed is the service's check,
# so it carries an error code. A post's optional ``scheduled_at`` is read as UTC
# when it has no offset, normalised to UTC, and must not be more than
# ``SCHEDULE_SKEW`` in the past. Learning loop: an experiment is created whole
# (extra keys refused, two distinct arms, format arms limited to X formats,
# ``min_posts_per_arm`` 1..50) and only ever closed with a verdict; a learning
# edit carries text (at most 300 characters) and/or a status. A brand rename
# (``RenameProfileRequest``) needs a trimmed, non-empty name of at most 120
# characters, the same cap as ``company_name``. A blueprint draft save replaces
# only the sections sent and refuses status, version and approval keys; pillar
# names, channels and goal ids are unique within the draft. A roadmap phase runs
# ``start`` <= ``end`` (inclusive days), its milestones fall inside it, its cadence
# names each channel once at 0..21 posts a week, and a milestone link is http(s).
# Phase and milestone ids are unique across the roadmap (the service mints a
# missing one). The weekly Plan (``WeekPlanResponse``) is a read only. A setup
# skip names one of the ``SetupKey`` rows (anything else is a 422).

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from pocketpaw_ee.cloud.growth.social.domain import (
    DEFAULT_IDEA_COUNT,
    MAX_BLUEPRINT_GOALS,
    MAX_BLUEPRINT_ITEMS,
    MAX_BLUEPRINT_LEARNINGS,
    MAX_BLUEPRINT_PILLARS,
    MAX_IDEA_COUNT,
    MAX_LEARNING_CHARS,
    MAX_PHASE_MILESTONES,
    MAX_POST_IMAGES,
    MAX_REPLY_CARDS_PER_DAY,
    MAX_REPLY_CHARS,
    MAX_REPLY_QUERIES,
    MAX_REPLY_QUERY_CHARS,
    MAX_REPLY_WATCH_HANDLES,
    MAX_ROADMAP_PHASES,
    MIN_POSTS_PER_ARM_DEFAULT,
    PLATFORM_FORMATS,
    AnalysisStatus,
    BlueprintChannelName,
    BlueprintStatus,
    BusinessModel,
    ExperimentMetric,
    ExperimentVariable,
    ExperimentVerdict,
    IdeaFormat,
    IdeaSource,
    IdeaStatus,
    LearningStatus,
    MilestoneStatus,
    MonthlyRevenue,
    Platform,
    ReplyStatus,
    SetupKey,
    SetupState,
    SocialRole,
    TeamSize,
    clean_tag,
)

MAX_SCRIPT_BEATS = 12
MAX_HASHTAGS = 15
MAX_ANALYSIS_ITEMS = 20
MAX_GITHUB_REPOS = 5
MAX_WEEKLY_POST_QUOTA = 21
SCHEDULE_SKEW = timedelta(minutes=1)
MAX_BLUEPRINT_POSTS_PER_WEEK = 50
MAX_BLUEPRINT_SENDS_PER_WEEK = 5000
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
    archived_at: str | None = None
    setup_skipped: list[str] = Field(default_factory=list)


class RenameProfileRequest(BaseModel):
    """Rename a brand: ``name`` becomes the profile's ``company_name``."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


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
    pillar: str | None = Field(default=None, max_length=200)
    hook_type: str | None = Field(default=None, max_length=200)

    @field_validator("pillar", "hook_type")
    @classmethod
    def _tag(cls, v: str | None) -> str | None:
        return clean_tag(v) if v is not None else v

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
                "script / hashtags / pillar / hook_type"
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
    pillar: str = ""
    hook_type: str = ""
    experiment_id: str = ""
    blueprint_version: int | None = None
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
    blueprint_version: int | None = None
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


# --- Learning loop: experiments, learnings and the weekly review ---------------------


class CreateExperimentRequest(BaseModel):
    """A new experiment. Nothing here can be changed after create; only the verdict moves."""

    model_config = ConfigDict(extra="forbid")

    hypothesis: str = Field(min_length=1, max_length=500)
    variable: ExperimentVariable
    arm_a: str = Field(min_length=1, max_length=200)
    arm_b: str = Field(min_length=1, max_length=200)
    metric: ExperimentMetric
    keep_rule: str = Field(default="", max_length=500)
    keep_threshold: float = 0.0
    min_posts_per_arm: int = Field(default=MIN_POSTS_PER_ARM_DEFAULT, ge=1, le=50)
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("hypothesis", "keep_rule")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("arm_a", "arm_b")
    @classmethod
    def _arm(cls, v: str) -> str:
        return clean_tag(v)

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)

    @model_validator(mode="after")
    def _check(self) -> CreateExperimentRequest:
        if not self.hypothesis:
            raise ValueError("hypothesis must not be blank")
        if not self.arm_a or not self.arm_b or self.arm_a == self.arm_b:
            raise ValueError("arm_a and arm_b must be two different, non-blank values")
        if self.variable == "format":
            allowed = PLATFORM_FORMATS["x"]
            if self.arm_a not in allowed or self.arm_b not in allowed:
                raise ValueError(f"format arms must be X formats: {', '.join(allowed)}")
        if self.start and self.end and self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class CloseExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["keep_a", "keep_b", "no_difference", "stopped"]


class SocialExperimentResponse(BaseModel):
    id: str
    workspace_id: str
    profile_id: str
    hypothesis: str
    variable: ExperimentVariable
    arm_a: str
    arm_b: str
    metric: ExperimentMetric
    keep_rule: str
    keep_threshold: float
    min_posts_per_arm: int
    start: str | None
    end: str | None
    verdict: ExperimentVerdict
    closed_at: str | None
    created_at: str | None


class SocialExperimentListResponse(BaseModel):
    items: list[SocialExperimentResponse]


class SocialLearningResponse(BaseModel):
    id: str
    workspace_id: str
    profile_id: str
    text: str
    source_experiment: str
    evidence: str
    status: LearningStatus
    approved_at: str | None
    retired_at: str | None
    created_at: str | None
    updated_at: str | None


class SocialLearningListResponse(BaseModel):
    items: list[SocialLearningResponse]


class CloseExperimentResponse(BaseModel):
    experiment: SocialExperimentResponse
    learning: SocialLearningResponse | None = None


class UpdateLearningRequest(BaseModel):
    """Edit a learning's text (while proposed or approved) and/or move its status."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=MAX_LEARNING_CHARS)
    status: Literal["approved", "retired"] | None = None

    @field_validator("text")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("text must not be blank")
        return v.strip() if v is not None else v

    @model_validator(mode="after")
    def _at_least_one(self) -> UpdateLearningRequest:
        if self.text is None and self.status is None:
            raise ValueError("provide text and/or status")
        return self


class ReviewPostResponse(BaseModel):
    idea_id: str
    hook: str
    format: str
    pillar: str
    hook_type: str
    experiment_id: str
    published_url: str
    published_at: str | None
    age_bucket: int | None
    impressions: int
    likes: int
    replies: int
    reposts: int
    quotes: int
    clicks: int
    clicks_per_1k_views: float | None
    engagement_rate: float | None
    repo_uniques_week: int


class TagAverageResponse(BaseModel):
    value: str
    posts: int
    clicks_per_1k_views: float | None
    engagement_rate: float | None


class ArmResponse(BaseModel):
    arm: Literal["a", "b"]
    value: str
    posts: int
    metric_average: float | None


class ReviewExperimentResponse(BaseModel):
    experiment: SocialExperimentResponse
    arms: list[ArmResponse]
    min_posts_met: bool


class RepoWeekResponse(BaseModel):
    repo: str
    stars: int
    stars_week_before: int
    uniques: int
    uniques_week_before: int


class SocialReviewResponse(BaseModel):
    profile_id: str
    week: str
    start: str
    end: str
    posts: list[ReviewPostResponse]
    tags: dict[str, list[TagAverageResponse]]
    experiment: ReviewExperimentResponse | None
    repos: list[RepoWeekResponse]


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
    # Strict allowlist: the return path is appended to the caller's origin, so
    # anything free-form would be an open redirect.
    return_to: (
        Literal["/growth/social/accounts", "/growth/next/settings", "/growth/settings"] | None
    ) = None


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


# --- Brand Blueprint ------------------------------------------------------------------------

_BlueprintText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
_BlueprintItem = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]
_BlueprintItems = Annotated[list[_BlueprintItem], Field(max_length=MAX_BLUEPRINT_ITEMS)]


class BlueprintIdentityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: _BlueprintText = ""
    product: _BlueprintText = ""
    problem: _BlueprintText = ""


class BlueprintAudienceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: _BlueprintItems = Field(default_factory=list)
    icp_sentences: _BlueprintItems = Field(default_factory=list)


class BlueprintVoiceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tone: _BlueprintText = ""
    avoid: _BlueprintItems = Field(default_factory=list)


class BlueprintPillarModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    weight: int = Field(default=0, ge=0, le=100)
    hooks: _BlueprintItems = Field(default_factory=list)


class BlueprintChannelModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: BlueprintChannelName
    posts_per_week: int = Field(default=0, ge=0, le=MAX_BLUEPRINT_POSTS_PER_WEEK)
    sends_per_week: int = Field(default=0, ge=0, le=MAX_BLUEPRINT_SENDS_PER_WEEK)


class BlueprintGoalInput(BaseModel):
    """An owner-entered goal. ``id`` is minted by the service when omitted."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    metric: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    target: float = Field(ge=0)
    channel: BlueprintChannelName | None = None


class BlueprintGoalModel(BaseModel):
    id: str
    metric: str
    target: float
    channel: BlueprintChannelName | None = None


_RoadmapId = Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)]


class RoadmapCadenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: BlueprintChannelName
    posts_per_week: int = Field(default=0, ge=0, le=MAX_WEEKLY_POST_QUOTA)


class RoadmapMilestoneModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: _RoadmapId = ""
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    date: date
    status: MilestoneStatus = "planned"
    link: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None

    @field_validator("link")
    @classmethod
    def _http_link(cls, v: str | None) -> str | None:
        if not v:
            return None
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("link must be an http(s) URL")
        return v


class RoadmapPhaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: _RoadmapId = ""
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    start: date
    end: date
    cadence: list[RoadmapCadenceModel] = Field(default_factory=list)
    goal_ids: Annotated[list[_RoadmapId], Field(max_length=MAX_BLUEPRINT_GOALS)] = Field(
        default_factory=list
    )
    milestones: Annotated[list[RoadmapMilestoneModel], Field(max_length=MAX_PHASE_MILESTONES)] = (
        Field(default_factory=list)
    )

    @model_validator(mode="after")
    def _dated(self) -> RoadmapPhaseModel:
        if self.start > self.end:
            raise ValueError("a phase must start on or before its end")
        for m in self.milestones:
            if not self.start <= m.date <= self.end:
                raise ValueError(f"milestone {m.title!r} falls outside its phase")
        _unique([c.channel for c in self.cadence], "cadence channels")
        return self


class BlueprintRoadmapModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phases: Annotated[list[RoadmapPhaseModel], Field(max_length=MAX_ROADMAP_PHASES)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _distinct(self) -> BlueprintRoadmapModel:
        _unique([p.id for p in self.phases if p.id], "phase ids")
        _unique([m.id for p in self.phases for m in p.milestones if m.id], "milestone ids")
        return self


_Pillars = Annotated[list[BlueprintPillarModel], Field(max_length=MAX_BLUEPRINT_PILLARS)]
_Goals = Annotated[list[BlueprintGoalInput], Field(max_length=MAX_BLUEPRINT_GOALS)]
_LearningIds = Annotated[
    list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]],
    Field(max_length=MAX_BLUEPRINT_LEARNINGS),
]


def _unique(values: list[str], what: str) -> None:
    lowered = [v.lower() for v in values]
    if len(set(lowered)) != len(lowered):
        raise ValueError(f"{what} must be unique")


class SaveBlueprintDraftRequest(BaseModel):
    """``PUT /blueprint/draft``: each section sent replaces that section of the draft;
    an omitted section is kept. ``learning_ids`` replaces the draft's learning ids
    (each must name a learning of the same brand, checked in the service). Status,
    version and approval are not editable here (extra keys are a 422)."""

    model_config = ConfigDict(extra="forbid")

    identity: BlueprintIdentityModel | None = None
    audience: BlueprintAudienceModel | None = None
    voice: BlueprintVoiceModel | None = None
    pillars: _Pillars | None = None
    channels: list[BlueprintChannelModel] | None = None
    goals: _Goals | None = None
    roadmap: BlueprintRoadmapModel | None = None
    learning_ids: _LearningIds | None = None

    @model_validator(mode="after")
    def _distinct(self) -> SaveBlueprintDraftRequest:
        if self.pillars is not None:
            _unique([p.name for p in self.pillars], "pillar names")
        if self.channels is not None:
            _unique([c.channel for c in self.channels], "channels")
        if self.goals is not None:
            _unique([g.id for g in self.goals if g.id], "goal ids")
        if self.learning_ids is not None:
            _unique(self.learning_ids, "learning ids")
        return self


class GrowthBlueprintResponse(BaseModel):
    id: str
    workspace_id: str
    profile_id: str
    version: int
    status: BlueprintStatus
    identity: BlueprintIdentityModel
    audience: BlueprintAudienceModel
    voice: BlueprintVoiceModel
    pillars: list[BlueprintPillarModel]
    channels: list[BlueprintChannelModel]
    goals: list[BlueprintGoalModel]
    roadmap: BlueprintRoadmapModel
    learning_ids: list[str]
    created_by: str
    approved_by: str | None
    approved_at: str | None
    created_at: str | None
    updated_at: str | None


class BlueprintStateResponse(BaseModel):
    """The profile's approved blueprint and its open draft; either may be null."""

    approved: GrowthBlueprintResponse | None
    draft: GrowthBlueprintResponse | None


class BlueprintSectionDiff(BaseModel):
    """Keys of one section: field names for identity/audience/voice, pillar names,
    channel names, goal ids or roadmap phase ids for the list sections."""

    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)


class BlueprintDiffResponse(BaseModel):
    from_version: int
    to_version: int
    sections: dict[str, BlueprintSectionDiff]


class SetupStatusRow(BaseModel):
    key: SetupKey
    state: SetupState


class SetupStatusResponse(BaseModel):
    """The brand's "Set up" checklist, rows in ``SETUP_KEYS`` order."""

    rows: list[SetupStatusRow]


class SkipSetupRequest(BaseModel):
    """Skip one checklist row, or un-skip it with ``skipped: false``."""

    key: SetupKey
    skipped: bool = True


# --- Weekly Plan ----------------------------------------------------------------------------


class PlanSlotModel(BaseModel):
    """One post slot: empty (``idea_id`` null), filled by a scheduled idea
    (``scheduled_at`` set) or by an approved idea waiting for a date."""

    channel: str
    idea_id: str | None = None
    scheduled_at: str | None = None


class PlanDayModel(BaseModel):
    date: str
    slots: list[PlanSlotModel]


class WeekPlanResponse(BaseModel):
    week: str
    days: list[PlanDayModel]
