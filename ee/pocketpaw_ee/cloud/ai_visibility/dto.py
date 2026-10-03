# AI visibility — DTOs.
#
# ``CheckResponse`` is one stored check as ``run_check`` returns it: the full
# internal view, never sent to an anonymous caller. The free public check
# (``POST /tools/ai-check``) speaks ``AiCheckRequest`` / ``AiCheckResponse``, both
# ``extra="forbid"`` so a field can only reach the public wire by being added here
# on purpose; every URL in the response is absolute https.

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


__all__ = [
    "AiCheckFix",
    "AiCheckRequest",
    "AiCheckResponse",
    "AiCheckSource",
    "CheckResponse",
    "SourceType",
]
