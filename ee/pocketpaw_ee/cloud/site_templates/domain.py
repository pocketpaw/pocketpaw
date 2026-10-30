# Site templates — domain value object.
#
# Frozen, built in service.py from a SiteTemplate doc. Tenancy (``workspace_id``)
# and ``owner`` are required at construction, with no defaults. The snapshot is
# deliberately NOT a field: nothing outside the service may hold a template's
# source, so the domain object carries the metadata only. It holds the real
# ``owner`` and ``workspace_id``; the service redacts them per viewer.
# ``kind``, ``audiences``, ``live_url``, ``tags`` and ``show_creator`` are the
# fields Discover lists the template under.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SiteTemplateMeta:
    """A saved site template's metadata (never its snapshot)."""

    workspace_id: str
    owner: str
    id: str
    name: str
    description: str
    visibility: str
    version: int
    engine: str | None
    pattern: str | None
    hidden: bool = False
    preview_image_url: str | None = None
    kind: str = "site"
    audiences: tuple[str, ...] = ()
    live_url: str | None = None
    tags: tuple[str, ...] = ()
    show_creator: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["SiteTemplateMeta"]
