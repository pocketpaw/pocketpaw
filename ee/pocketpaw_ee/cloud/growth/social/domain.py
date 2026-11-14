# ee/pocketpaw_ee/cloud/growth/social/domain.py — frozen value objects and the
# vocabularies for Growth › Social. Pure Python (no Beanie / Pydantic /
# FastAPI), so the service, both agents and the import-linter "Growth"
# contract can depend on it freely. Value objects carry ``workspace_id``:
# tenancy is enforced at construction.
#
# The Literals are the API contract's enums verbatim
# (docs/design/drafts/2026-10-06-growth-social-setup.md); the frontend builds
# against the same strings, so a rename here is a breaking change there.
# ``COMPLETE_REQUIRED_FIELDS`` is what ``POST /social/profile/complete`` demands.
# ``POSTABLE_PLATFORMS`` are the connected accounts "post now" can publish to;
# ``CHANNEL_PLATFORMS`` additionally need a channel picked per post and
# ``VIDEO_PLATFORMS`` accept video. Media must sit at a public http(s) URL
# (``is_public_media_url``): Slack fetches it itself and Postiz's X provider
# refuses private hosts. ``MEDIA_EXTENSIONS`` is the upload allowlist.
# ``DAILY_SLOT_HOURS_UTC`` and ``DAILY_DRAFTS_PER_DAY`` shape the daily X
# drafting sweep; an idea's ``source`` says whether a person or that sweep
# asked for it, and its ``post_ids`` are the Postiz posts made from it.
# ``METRIC_AGE_BUCKETS`` are the post ages (days) a ``PostMetrics`` snapshot of
# a published X post is taken at. Reply cards: ``ReplyCandidate`` is an X post
# found to reply to, ``GeneratedReply`` a drafted reply, ``ReplyCard`` the stored
# card (never posted by the server); the ``REPLY_*`` / ``MAX_REPLY_*`` constants
# bound a profile's reply settings.

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, get_args
from urllib.parse import urlsplit

TeamSize = Literal["solo", "2_10", "11_50", "51_200", "200_plus"]
MonthlyRevenue = Literal["pre_revenue", "under_1k", "1k_10k", "10k_50k", "50k_250k", "250k_plus"]
SocialRole = Literal["founder", "marketer", "social_media_manager", "agency", "creator", "other"]
BusinessModel = Literal[
    "b2b_saas",
    "b2c_app",
    "ecommerce",
    "services",
    "local_business",
    "creator",
    "marketplace",
    "other",
]
AnalysisStatus = Literal["none", "ready", "failed"]
IdeaFormat = Literal[
    "hook_demo",
    "slideshow",
    "wall_of_text",
    "meme",
    "talking_head",
    "x_post",
    "x_thread",
    "reddit_post",
]
Platform = Literal["x", "reddit"]
IdeaStatus = Literal["new", "approved", "skipped"]
IdeaSource = Literal["manual", "daily"]
ReplyStatus = Literal["new", "posted", "skipped"]

IDEA_FORMATS: tuple[str, ...] = get_args(IdeaFormat)
PLATFORMS: tuple[str, ...] = get_args(Platform)
VIDEO_FORMATS: tuple[str, ...] = (
    "hook_demo",
    "slideshow",
    "wall_of_text",
    "meme",
    "talking_head",
)
PLATFORM_FORMATS: dict[str, tuple[str, ...]] = {
    "x": ("x_post", "x_thread"),
    "reddit": ("reddit_post",),
}

DESCRIPTION_FIELDS: tuple[str, ...] = (
    "product",
    "audience",
    "problem",
    "benefits",
    "tone",
    "avoid",
)

COMPLETE_REQUIRED_FIELDS: tuple[str, ...] = (
    "owner_name",
    "company_name",
    "team_size",
    "monthly_revenue",
    "role",
    "business_model",
    "category",
)

SOCIAL_ACCOUNT_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("x", "X"),
    ("linkedin", "LinkedIn"),
    ("linkedin-page", "LinkedIn Page"),
    ("facebook", "Facebook Page"),
    ("instagram-standalone", "Instagram"),
    ("threads", "Threads"),
    ("youtube", "YouTube"),
    ("tiktok", "TikTok"),
    ("reddit", "Reddit"),
    ("pinterest", "Pinterest"),
    ("dribbble", "Dribbble"),
    ("slack", "Slack"),
    ("telegram", "Telegram"),
)
POSTABLE_PLATFORMS = frozenset({"slack", "x"})
CHANNEL_PLATFORMS = frozenset({"slack"})
VIDEO_PLATFORMS = frozenset({"x"})
MAX_POST_IMAGES = 4
MEDIA_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
}


def media_kind(path: str) -> str:
    return "video" if urlsplit(path).path.lower().endswith(".mp4") else "image"


def is_public_media_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


DEFAULT_IDEA_COUNT = 6
# The daily X drafts: at most this many a day per profile, each suggested for the next
# unused one of these UTC hours (rolling to tomorrow once today's has passed).
DAILY_SLOT_HOURS_UTC: tuple[int, ...] = (14, 17)
DAILY_DRAFTS_PER_DAY = 2
METRIC_AGE_BUCKETS: tuple[int, ...] = (1, 3, 7, 28)
# Reply cards: per-profile settings bounds and the X post length.
REPLY_CARDS_PER_DAY_DEFAULT = 15
MAX_REPLY_CARDS_PER_DAY = 25
MAX_REPLY_WATCH_HANDLES = 30
MAX_REPLY_QUERIES = 5
MAX_REPLY_QUERY_CHARS = 200
MAX_REPLY_CHARS = 280
MAX_IDEA_COUNT = 12
# Idea tags (``pillar``, ``hook_type``): short free text the weekly review averages over.
MAX_TAG_CHARS = 40


def clean_tag(value: object) -> str:
    """A tag trimmed, inner whitespace squashed, capped at ``MAX_TAG_CHARS``."""
    return " ".join(value.split())[:MAX_TAG_CHARS] if isinstance(value, str) else ""


@dataclass(frozen=True)
class SocialAnalysis:
    """What the analyst concluded about the company. ``pages_read`` and
    ``logo_url`` come from the fetch, never from the model."""

    summary: str = ""
    product: str = ""
    audience: str = ""
    problem: str = ""
    tone: str = ""
    benefits: tuple[str, ...] = ()
    differentiators: tuple[str, ...] = ()
    competitors: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()
    content_pillars: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    pages_read: tuple[str, ...] = ()
    logo_url: str | None = None


@dataclass(frozen=True)
class SocialProfile:
    id: str
    workspace_id: str
    owner_name: str = ""
    company_name: str = ""
    website: str | None = None
    description: dict[str, str] = field(default_factory=dict)
    team_size: str | None = None
    monthly_revenue: str | None = None
    role: str | None = None
    business_model: str | None = None
    category: str | None = None
    analysis_status: str = "none"
    analysis_error: str | None = None
    analysis: SocialAnalysis | None = None
    analyzed_at: datetime | None = None
    onboarding_completed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    characters: tuple[dict[str, str], ...] = ()
    active: bool = False
    weekly_post_quota: int = 5
    github_repos: tuple[str, ...] = ()
    reply_watch_handles: tuple[str, ...] = ()
    reply_queries: tuple[str, ...] = ()
    reply_cards_per_day: int = REPLY_CARDS_PER_DAY_DEFAULT

    def has_description(self) -> bool:
        return any((self.description.get(key) or "").strip() for key in DESCRIPTION_FIELDS)

    def missing_for_completion(self) -> list[str]:
        missing: list[str] = []
        for name in COMPLETE_REQUIRED_FIELDS:
            value = getattr(self, name)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(name)
        return missing


@dataclass(frozen=True)
class SocialIdea:
    id: str
    workspace_id: str
    format: str
    hook: str
    on_screen_text: str = ""
    caption: str = ""
    why: str = ""
    script: tuple[str, ...] = ()
    hashtags: tuple[str, ...] = ()
    platform: str = ""
    subreddit: str = ""
    status: str = "new"
    scheduled_at: datetime | None = None
    calendar_event_id: str = ""
    poster_svg: str = ""
    reel_html: str = ""
    media_choice: str = ""
    source: str = "manual"
    suggested_at: datetime | None = None
    post_ids: tuple[str, ...] = ()
    published_url: str = ""
    published_at: datetime | None = None
    x_post_id: str = ""
    pillar: str = ""
    hook_type: str = ""
    experiment_id: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class GeneratedIdea:
    """One idea as the ideas agent proposed it, before it is stored."""

    format: str
    hook: str
    on_screen_text: str = ""
    caption: str = ""
    why: str = ""
    script: tuple[str, ...] = ()
    hashtags: tuple[str, ...] = ()
    platform: str = ""
    subreddit: str = ""
    pillar: str = ""
    hook_type: str = ""


@dataclass(frozen=True)
class AnalysisRequest:
    """Everything the analyser gets: the website (or None) and what the user typed."""

    workspace_id: str
    company_name: str
    website: str | None
    description: dict[str, str]


@dataclass(frozen=True)
class AnalysisOutcome:
    """An analysis, or a short human reason there is none."""

    analysis: SocialAnalysis | None = None
    error: str | None = None


@dataclass(frozen=True)
class PostMetrics:
    """One published X post's public metrics at ``age_bucket`` days old."""

    workspace_id: str
    idea_id: str
    x_post_id: str
    age_bucket: int
    impressions: int = 0
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    quotes: int = 0
    bookmarks: int = 0
    captured_at: datetime | None = None


@dataclass(frozen=True)
class ReplyCandidate:
    """A recent X post someone might reply to, as read from X."""

    post_id: str
    author: str
    text: str
    url: str


@dataclass(frozen=True)
class GeneratedReply:
    """One reply as the drafting agent proposed it, before it is checked and stored."""

    post_id: str
    text: str
    why: str = ""


@dataclass(frozen=True)
class ReplyCard:
    """A stored reply card. A person posts it by hand from ``intent_url``, or skips it."""

    id: str
    workspace_id: str
    profile_id: str
    target_post_id: str
    platform: str = "x"
    target_url: str = ""
    author: str = ""
    context: str = ""
    draft_text: str = ""
    intent_url: str = ""
    why: str = ""
    status: str = "new"
    created_at: datetime | None = None
    updated_at: datetime | None = None
