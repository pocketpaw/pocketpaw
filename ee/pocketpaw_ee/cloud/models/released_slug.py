# ee/pocketpaw_ee/cloud/models/released_slug.py — an address a site just gave up.
# Created: 2026-09-23 (VS-4, feat/sites-rename). Written and read ONLY through
# ``pocketpaw_ee.sites.service`` (the rename apply step and the availability check).
#
# When a site renames (``acme-bakery`` -> ``acme-cakes``), its old address stops
# serving on the next publish. Handing ``acme-bakery`` to a stranger the same minute
# would let them publish at the URL the old owner's customers, links and QR codes
# still point at. So the old name is HELD for 30 days:
#
#   * unavailable to every OTHER workspace until ``hold_until``;
#   * available to the workspace that released it, so an owner who regrets a rename
#     can take the name back;
#   * an expired hold counts as free, and a successful claim deletes the row.
#
# ``paw-site-<id>`` names are never held: they are reserved (``paw-`` prefix) and no
# customer could claim one anyway.
#
# One row per slug (unique index), so a second release of the same name REPLACES the
# hold rather than stacking two.

from __future__ import annotations

from datetime import datetime

from beanie import Document
from pymongo import IndexModel

# How long a released address stays with the workspace that gave it up.
HOLD_DAYS = 30


class ReleasedSlug(Document):
    """A site address recently given up by a rename, held for its old workspace."""

    slug: str
    site_id: str
    workspace_id: str
    released_at: datetime
    hold_until: datetime

    class Settings:
        name = "released_slugs"
        indexes = [
            IndexModel([("slug", 1)], unique=True, name="uq_released_slug"),
        ]


__all__ = ["HOLD_DAYS", "ReleasedSlug"]
