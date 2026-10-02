# Discover — domain value object.
#
# Created 2026-10-01 (feat/discover-index): a frozen view of one listing, built in
# ``service_admin.py`` from a DiscoverListing doc. Tenancy (``workspace_id``) and
# ``owner`` are required with no defaults; the public wire mapping
# (``service_admin._public``) deliberately drops them, along with ``hidden`` and
# ``source_id``. Reports never enter the view.
#
# Updated 2026-10-02 (feat/studio-templates): ``media_kind`` / ``media_url``.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DiscoverListingView:
    """One Discover listing (never its reports)."""

    workspace_id: str
    owner: str
    id: str
    source: str
    source_id: str
    kind: str
    title: str
    description: str
    audiences: tuple[str, ...]
    preview_image_url: str | None
    live_url: str | None
    featured: bool
    hidden: bool
    remix_count: int
    created_at: datetime | None = None
    media_kind: str | None = None
    media_url: str | None = None


__all__ = ["DiscoverListingView"]
