# ee/pocketpaw_ee/sites/design_brief.py — build the site-authoring crew's
# ``DesignBrief`` from a crawled URL, so a rebuild-mode import feeds the SAME
# baton the crew's Frontend stage already knows how to build from.
#
# REWRITTEN 2026-09-04 (feat/sites-import-regenerate). The first version of this
# module declared its own ``DesignBrief``, which was a duplicate: the codebase
# already had one at ``sites_crew/models.py``, threaded Designer → Branding →
# Frontend, and ``cloud/surface/handlers/sites.py::_frontend_preamble`` already
# renders build instructions from it and routes to ``create_svelte_site``. That
# preamble has been written and unwired since 2026-07-06, waiting on an
# orchestration slice that never landed. Regenerating from a URL is that slice.
#
# So there is no second brief type. This module only knows how to FILL the crew's
# brief from a harvest, and how to version the persisted copy — the crew model
# carries no version of its own because, until now, it never outlived a request.
#
# Created 2026-09-04 (IR-2a).
"""Build the crew's DesignBrief from a crawled source page."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from pocketpaw_ee.sites_crew.models import (
    AssetRef,
    Branding,
    DesignBrief,
    DesignDirection,
    DesignSystem,
)

# Bump on any change to how a brief is PERSISTED that an older reader would
# misinterpret. This lives here rather than on the crew model because the crew
# model is a request-scoped baton; only the import path stores one and has to
# read it back later.
BRIEF_VERSION = 1

# The section roles the crew's ``Section.role`` already names, which are also
# what the design-taste skill authors. IR-3 classifies into this set; anything it
# cannot place becomes "custom" plus an open question, never a dropped band.
SECTION_ROLES = (
    "nav",
    "hero",
    "services",
    "proof",
    "pricing",
    "cta",
    "lead_form",
    "faq",
    "footer",
    "custom",
)


class BriefVersionError(ValueError):
    """A stored brief cannot be read by this build. Recapture, or upgrade."""


def build_brief_from_source(
    *,
    source_url: str,
    title: str = "",
    description: str = "",
    favicon_url: str | None = None,
    warnings: list[str] | None = None,
) -> DesignBrief:
    """Fill the crew brief from what a capture learned about a source page.

    IR-2a fills the identity layer only. The sitemap (IR-3), the design system
    (IR-4) and the asset manifest (IR-5) are each a later slice and land empty,
    which the crew model already tolerates — every one of those fields has a
    default, because the crew fills them stage by stage too.

    ``engine`` is svelte because that is the track with the native edit lane, and
    being able to restructure the result is the entire reason a rebuild exists
    rather than a mirror.
    """
    host = urlparse(source_url).netloc or source_url
    subject = title.strip() or host
    # The TITLE leads, because it is the site's name and the host is only its
    # address. A goal that says "rebuild rohitk06.in" tells the agent where the
    # reference lives; one that says "rebuild Rohit Kushwaha (rohitk06.in)" tells
    # it what it is building.
    named = f"{subject} ({host})" if subject != host else host
    goal = (
        f"Rebuild {named} as a native Paw site. Match the source's structure, "
        f"layout and design language; do not copy its markup."
    )
    if description.strip():
        goal += f" The source describes itself as: {description.strip()}"

    # AssetRef REJECTS anything that is not an http(s) URL, which is the right
    # rule and also why this is guarded rather than passed straight through: a
    # page can declare a data: favicon, and a source that declares none is
    # ordinary. Neither is worth failing a capture over.
    favicon = None
    if favicon_url and favicon_url.startswith(("http://", "https://")):
        favicon = AssetRef(url=favicon_url, kind="favicon", alt=f"{subject} favicon")
    elif favicon_url:
        (warnings := list(warnings or [])).append(
            "the source's favicon is not a fetchable http(s) URL and was not carried over"
        )

    brand = Branding(
        design_system=DesignSystem(name=f"{subject} (from {host})"),
        favicon_asset=favicon,
    )
    return DesignBrief(
        goal=goal,
        engine="svelte",
        pattern="landing",
        # The source URL is a visual REFERENCE, which is exactly what this field
        # is for. It is not an asset and not a page we are hosting.
        design_direction=DesignDirection(
            references=[source_url],
            layout_notes="Match the source page's section order and proportions.",
        ),
        branding=brand,
        open_questions=list(warnings or []),
    )


def dump_brief(brief: DesignBrief) -> dict[str, Any]:
    """The persisted envelope: the crew brief plus the version that wrote it."""
    return {"version": BRIEF_VERSION, "brief": brief.model_dump(mode="json")}


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
    if version != BRIEF_VERSION:
        direction = "newer than" if version > BRIEF_VERSION else "older than"
        remedy = (
            "upgrade rather than reading it" if version > BRIEF_VERSION else "recapture the source"
        )
        raise BriefVersionError(
            f"design brief is version {version}, {direction} this build reads "
            f"({BRIEF_VERSION}); {remedy}"
        )
    body = payload.get("brief")
    if not isinstance(body, Mapping):
        raise BriefVersionError("design brief envelope carries no brief")
    return DesignBrief.model_validate(dict(body))
