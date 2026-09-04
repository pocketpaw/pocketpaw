# ee/pocketpaw_ee/sites/design_brief.py — the typed, versioned description of a
# source page that the SITE REGENERATION path hands to the generator, instead of
# re-hosting the source's own bytes the way the html mirror does.
#
# Created 2026-09-04 (IR-2a, feat/sites-import-design-brief). Only ``meta`` is
# filled at capture time; ``tokens`` (IR-4), ``sections`` (IR-3), ``forms`` (IR-9)
# and ``assets`` (IR-5) are each filled by a later slice and land empty here. They
# exist now so the persisted shape does not change under a stored brief every time
# one of those slices lands.
#
# WHY A VERSION FIELD, CHECKED ON READ: a brief outlives the capture that made it
# — the whole point of persisting one is that a regenerate does not re-crawl. So a
# brief written by an older build will be read by a newer one. ``load_brief``
# refuses a mismatch loudly (recapture, or a build that is too old to read this)
# rather than letting pydantic quietly default away fields whose meaning changed.
# A silently misread brief regenerates a site that looks nothing like its source
# and reports no error, which is the failure mode this file exists to prevent.
"""Typed design brief for regenerating a site from a URL."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Bump on ANY change to the shape below that an older reader would misinterpret.
# Adding a field with a default that reads correctly as "absent" does not need a
# bump; renaming, re-typing, or changing what a field MEANS does.
BRIEF_VERSION = 1

# The closed section vocabulary. Deliberately the set the design-taste skill
# already authors, so a brief never asks the generator for a section kind it has
# no idiom for. Anything unrecognised becomes ``content`` plus a warning — see
# IR-3 — because dropping a section silently loses a whole band of the page.
SECTION_KINDS = (
    "hero",
    "features",
    "pricing",
    "testimonial",
    "faq",
    "cta",
    "footer",
    "content",
)


class BriefVersionError(ValueError):
    """A stored brief cannot be read by this build. Recapture, or upgrade."""


class BriefMeta(BaseModel):
    """What the source page says it is. The only family filled at capture time."""

    title: str = ""
    description: str = ""
    favicon_url: str | None = None
    og_image_url: str | None = None


class BriefTokens(BaseModel):
    """The source's design language. Filled by IR-4 from its stylesheets.

    ``palette`` is ORDERED most-referenced first, which is the whole reason it is
    a list rather than a set: a real site declares dozens to hundreds of colour
    tokens and a brief needs a handful, so the order is the selection.
    """

    palette: list[str] = Field(default_factory=list)
    # {"heading": <family>, "body": <family>, "mono": <family>} — resolved
    # families, never a raw ``var(--x)`` reference and never a bare fallback stack.
    fonts: dict[str, str] = Field(default_factory=dict)
    type_scale: list[str] = Field(default_factory=list)
    spacing: list[str] = Field(default_factory=list)
    radii: list[str] = Field(default_factory=list)
    shadows: list[str] = Field(default_factory=list)


class BriefSection(BaseModel):
    """One band of the source page, in document order. Filled by IR-3."""

    kind: str = "content"
    order: int = 0
    heading: str = ""
    subcopy: str = ""
    items: list[str] = Field(default_factory=list)
    image_refs: list[str] = Field(default_factory=list)
    # top / left / width / height as the rendered page reported them. Kept because
    # the ORDER and relative size of bands is most of what "same layout" means.
    geometry: dict[str, float] = Field(default_factory=dict)


class BriefForm(BaseModel):
    """A form the source page carried. Filled by IR-9.

    ``fields`` carries the original NAMES because downstream lead handling keys on
    them: a regenerated form with prettier field names captures leads that no
    existing mapping recognises.
    """

    purpose: str = ""
    fields: list[str] = Field(default_factory=list)
    original_action: str = ""
    method: str = "post"


class DesignBrief(BaseModel):
    """Everything the generator needs to author a native site that reads as the
    source's, without ever taking ownership of the source's own tree."""

    version: int = BRIEF_VERSION
    source_url: str
    captured_at: datetime
    meta: BriefMeta = Field(default_factory=BriefMeta)
    tokens: BriefTokens = Field(default_factory=BriefTokens)
    sections: list[BriefSection] = Field(default_factory=list)
    forms: list[BriefForm] = Field(default_factory=list)
    # path -> the URL it was stored at in OUR blob storage, so a generated section
    # never points at the original host. Filled by IR-5.
    assets: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def load_brief(payload: Mapping[str, Any]) -> DesignBrief:
    """Read a persisted brief, refusing a version this build cannot read.

    Raises ``BriefVersionError`` rather than validating a mismatched payload: a
    brief is persisted input to a generation run, so a quietly-defaulted field
    produces a plausible site that is wrong about its source and reports nothing.
    """
    stored = payload.get("version")
    try:
        version = int(stored)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise BriefVersionError(
            f"design brief has no readable version (got {stored!r}); recapture the source"
        ) from None
    if version > BRIEF_VERSION:
        raise BriefVersionError(
            f"design brief is version {version}, newer than this build reads "
            f"({BRIEF_VERSION}); upgrade rather than reading it"
        )
    if version < BRIEF_VERSION:
        raise BriefVersionError(
            f"design brief is version {version}, older than this build reads "
            f"({BRIEF_VERSION}); recapture the source"
        )
    return DesignBrief.model_validate(dict(payload))
