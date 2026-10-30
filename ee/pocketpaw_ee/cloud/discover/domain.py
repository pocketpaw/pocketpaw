# Discover — domain value object.
#
# A frozen view of one listing, built in ``service_admin._view`` from a
# DiscoverListing doc. Tenancy (``workspace_id``) and ``owner`` are required with
# no defaults; the public wire mapping (``service_admin._public``) deliberately
# drops them, along with ``hidden``, ``source_id``, ``show_creator`` and
# ``remixed_from_source_id`` (the last two are resolved into the public
# ``creator`` / ``remixed_from`` instead). Reports never enter the view.
# ``slug`` is always a string here: a row not yet backfilled reports its id,
# which the public item route accepts in the slug's place.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DiscoverListingView:
    """One Discover listing (never its reports)."""

    workspace_id: str
    owner: str
    id: str
    slug: str
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
    updated_at: datetime | None = None
    tags: tuple[str, ...] = ()
    show_creator: bool = False
    remixed_from_source_id: str | None = None
    media_kind: str | None = None
    media_url: str | None = None


__all__ = ["DiscoverListingView"]
