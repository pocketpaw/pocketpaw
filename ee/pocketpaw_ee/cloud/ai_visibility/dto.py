# AI visibility — DTOs.
#
# ``CheckResponse`` is one stored check as ``run_check`` returns it: the full
# internal view, never sent to an anonymous caller. The free public check
# (``POST /tools/ai-check``) speaks ``AiCheckRequest`` / ``AiCheckResponse``, both
# ``extra="forbid"`` so a field can only reach the public wire by being added here
# on purpose; every URL in the response is absolute https. The Staff card
# (``/sites/{id}/ai-visibility``) reads ``SiteVisibilityResponse`` and writes
# ``SetQuestionsRequest`` / ``ApplyFixRequest``.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

SourceType = Literal["own_site", "gbp", "yelp", "tripadvisor", "reddit", "directory", "other"]


class CheckResponse(BaseModel):
    id: str
    workspace_id: str | None
    site_id: str | None
    business: dict[str, Any]
    location: dict[str, Any]
    questions: list[str]
    #: One row per engine x question x sample; failed calls have ``ok=False``.
    runs: list[dict[str, Any]]
    #: Per engine: ``{named, of, failed, near_miss, competitors}``. ``of`` counts
    #: answers received, so "named X of N" never counts a failed call.
    summary: dict[str, Any]
    fix: dict[str, Any]
    total_cost_usd: float
    created_at: datetime | None = None


class AiCheckRequest(BaseModel):
    """Body of the free public check. ``website`` is reduced to its host."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=100)
    city: str = Field(min_length=2, max_length=100)
    website: str | None = Field(default=None, max_length=300)
    turnstile_token: str = Field(min_length=1, max_length=4096)

    @field_validator("website")
    @classmethod
    def _website_host(cls, value: str | None) -> str | None:
        if not value:
            return None
        parts = urlsplit(value if "://" in value else f"https://{value}")
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or "." not in host:
            raise ValueError("website must be a web address like example.com")
        return host


class AiCheckSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: SourceType
    url: str


class AiCheckFix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str


class AiCheckResponse(BaseModel):
    """The public result: mentioned or not, never a count or a rank."""

    model_config = ConfigDict(extra="forbid")

    mentioned: bool
    engine: Literal["ChatGPT"] = "ChatGPT"
    answers_checked: int
    competitors: list[str]
    sources: list[AiCheckSource]
    fix: AiCheckFix


class SetQuestionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[str] = Field(min_length=1, max_length=10)

    @field_validator("questions")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        out = [q.strip() for q in value]
        if any(not 3 <= len(q) <= 200 for q in out):
            raise ValueError("each question must be 3 to 200 characters")
        return out


class ApplyFixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fix_id: str = Field(min_length=1, max_length=40)


class EngineSummary(BaseModel):
    label: str
    named: int
    of: int
    failed: int


class NameCount(BaseModel):
    name: str
    count: int


class TypeCount(BaseModel):
    type: SourceType
    count: int


class SiteFix(BaseModel):
    id: str
    text: str
    we_can_apply: bool


class SiteCheck(BaseModel):
    status: Literal["pending", "running", "done", "failed"]
    ran_at: datetime | None
    next_run_at: datetime | None
    questions: list[str]
    engines: list[EngineSummary]
    competitors: list[NameCount]
    sources: list[TypeCount]
    fix: SiteFix | None


class SiteVisibilityResponse(BaseModel):
    """The Staff card. ``questions`` is what the next check will ask (the
    owner's list); ``check.questions`` is what the shown check asked."""

    ai_training_allowed: bool
    plan_allows_check: bool
    questions: list[str]
    check: SiteCheck | None


class CheckQueuedResponse(BaseModel):
    status: Literal["pending"] = "pending"


class ApplyFixResponse(BaseModel):
    republish: Literal["started"] = "started"


__all__ = [
    "AiCheckFix",
    "AiCheckRequest",
    "AiCheckResponse",
    "AiCheckSource",
    "ApplyFixRequest",
    "ApplyFixResponse",
    "CheckQueuedResponse",
    "CheckResponse",
    "EngineSummary",
    "NameCount",
    "SetQuestionsRequest",
    "SiteCheck",
    "SiteFix",
    "SiteVisibilityResponse",
    "SourceType",
    "TypeCount",
]
