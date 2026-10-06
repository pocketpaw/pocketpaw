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
# is free text up to 60 characters. Idea edits need at least one field; script
# beats and hashtags are trimmed and capped.

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from pocketpaw_ee.cloud.growth.social.domain import (
    DEFAULT_IDEA_COUNT,
    MAX_IDEA_COUNT,
    AnalysisStatus,
    BusinessModel,
    IdeaFormat,
    IdeaStatus,
    MonthlyRevenue,
    Platform,
    SocialRole,
    TeamSize,
)

MAX_SCRIPT_BEATS = 12
MAX_HASHTAGS = 15
MAX_ANALYSIS_ITEMS = 20

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
    ``company_name`` clear to ``""``)."""

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
    created_at: str | None
    updated_at: str | None


class SocialIdeaListResponse(BaseModel):
    items: list[SocialIdeaResponse]


class SocialProfileListResponse(BaseModel):
    items: list[SocialProfileResponse]
