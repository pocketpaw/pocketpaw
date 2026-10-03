# AI visibility — domain value objects.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3): the business being checked
# (with aliases, website domain and competitors), the location the question is
# asked from, one engine answer, and one mention judgement. Frozen dataclasses;
# ``service.run_check`` stores them on the AiVisibilityCheck doc via ``asdict``.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Competitor:
    name: str
    aliases: tuple[str, ...] = ()
    domain: str | None = None


@dataclass(frozen=True)
class Business:
    """The business we look for in answers. ``domain`` is its own website
    (``joespizza.com``), used to spot its site among cited / consulted URLs."""

    name: str
    business_type: str
    aliases: tuple[str, ...] = ()
    domain: str | None = None
    competitors: tuple[Competitor, ...] = ()


@dataclass(frozen=True)
class Location:
    """Where the question is asked from. Passed to each engine's
    ``user_location``; ``area`` is a neighbourhood used by question templates."""

    city: str
    country: str  # ISO 3166-1 alpha-2
    region: str | None = None
    area: str | None = None
    timezone: str | None = None  # IANA id
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class EngineAnswer:
    """One answer from one engine. ``cited_urls`` are the links the answer shows;
    ``consulted_urls`` every URL the engine read (a superset, when exposed)."""

    engine: str
    model: str
    text: str
    cited_urls: tuple[str, ...] = ()
    consulted_urls: tuple[str, ...] = ()
    raw_usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0


@dataclass(frozen=True)
class MentionJudgement:
    """How an answer talks about the business. ``sentiment`` 0 (very negative)
    to 4 (very positive); ``judged_by`` names the decision model used."""

    position: str  # recommended | listed | negative
    sentiment: int
    confidence: float
    judged_by: str


__all__ = ["Business", "Competitor", "EngineAnswer", "Location", "MentionJudgement"]
