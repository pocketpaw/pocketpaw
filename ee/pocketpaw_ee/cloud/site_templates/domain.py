# Site templates — domain value object.
#
# Frozen, built in service.py from a SiteTemplate doc. Tenancy (``workspace_id``)
# and ``owner`` are required at construction, with no defaults. The snapshot is
# deliberately NOT a field: nothing outside the service may hold a template's
# source, so the domain object carries the metadata only. It holds the real
# ``owner`` and ``workspace_id``; the service redacts them per viewer.

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
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["SiteTemplateMeta"]
