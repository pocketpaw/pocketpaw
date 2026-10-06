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

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, get_args

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

DEFAULT_IDEA_COUNT = 6
MAX_IDEA_COUNT = 12


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
