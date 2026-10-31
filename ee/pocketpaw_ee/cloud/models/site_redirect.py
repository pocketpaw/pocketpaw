# ee/pocketpaw_ee/cloud/models/site_redirect.py — an old site address that now
# redirects. Written and read ONLY through ``pocketpaw_ee.sites.slug_redirect``.
#
# When a site renames (``acme-bakery`` -> ``acme-cakes``), the Worker under the old
# name is replaced by a tiny redirect Worker that answers every request with a 301 to
# the same path and query on the site's CURRENT address. One row per old name:
#
#   * ``name`` is the Worker script (= the workers.dev subdomain) that redirects;
#   * ``target_url`` is the origin it sends people to. Always the site's current
#     address: a later rename re-points every row of the site, so A -> B -> C leaves
#     A and B both sending straight to C;
#   * ``expires_at`` is when the redirect Worker is deleted and the name freed, the
#     same moment the old name's ``ReleasedSlug`` hold ends.
#
# A site that takes an old name back deletes the row (its own deploy replaced the
# redirect Worker); deleting the site deletes every row and redirect Worker it owns.

from __future__ import annotations

from datetime import datetime

from beanie import Document
from pymongo import IndexModel


class SiteRedirect(Document):
    """A renamed site's old address, redirecting to its current one until it expires."""

    name: str
    site_id: str
    workspace_id: str
    target_url: str
    created_at: datetime
    expires_at: datetime

    class Settings:
        name = "site_redirects"
        indexes = [
            IndexModel([("name", 1)], unique=True, name="uq_site_redirect_name"),
            IndexModel([("site_id", 1)], name="site_redirect_site"),
            IndexModel([("expires_at", 1)], name="site_redirect_expiry"),
        ]


__all__ = ["SiteRedirect"]
