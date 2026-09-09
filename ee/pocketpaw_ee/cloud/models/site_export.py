# ee/pocketpaw_ee/cloud/models/site_export.py — the data a site's owner keeps when
# the site itself is destroyed. Workspace-scoped.
#
# Created 2026-09-08 (sites lifecycle wave 1 chunk 2, feat/sites-delete-export).
#
# WHY THIS IS ITS OWN COLLECTION AND NOT A FIELD ON THE SITE. The delete cascade
# ends by deleting the Site document, so anything recorded ON that document dies
# with it — including a pointer to the export. That would orphan the export at the
# exact moment it becomes the only surviving copy of the customer's data, which is
# the opposite of what taking it was for. The export therefore outlives its site by
# construction, and every field a reader needs to identify it later is DENORMALISED
# here (``site_name``, ``pocket_id``, ``site_url``) rather than joined: after the
# cascade there is nothing left to join to, and an export that cannot say which site
# it came from is not a recovery story.
#
# ``status`` exists for the same reason it does on SiteDesignBrief — three states
# that look identical from outside otherwise: the build has not run, it ran and
# failed, or it ran and there are bytes to download. The delete cascade treats only
# ``ready`` as satisfying its precondition, so a failed export BLOCKS the destroy
# rather than letting it proceed over data nobody kept.
"""One exported copy of a site's data, taken before the site is destroyed."""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SiteExport(TimestampedDocument):
    """A site's data, captured to blob storage so a delete cannot lose it.

    The bytes live in the PRIVATE artifact adapter, never the public asset rail:
    that rail is world-readable and immutably cached, which is correct for a hero
    image and catastrophic for someone's booking records. ``storage_key`` is an
    internal key, NOT a URL — reads go through the authenticated download endpoint,
    which re-checks the workspace on every request. Nothing about this document is
    a bearer token.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    owner: str

    # ── Identity of a site that may no longer exist ──────────────────────
    # All denormalised on purpose. Once the cascade completes there is no Site
    # document, no pocket, and no deployed URL to resolve any of this from.
    site_id: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str = ""
    site_name: str = ""
    site_url: str = ""

    # ── The payload ──────────────────────────────────────────────────────
    status: str = "pending"  # pending | ready | failed
    # SAFE text only. Every site reader in the workspace can see this, so it
    # carries a sentence we wrote, never a raw driver or Cloudflare error.
    error: str = ""
    storage_key: str = ""
    size_bytes: int = 0

    # What the export actually contains, recorded so the confirm dialog can show
    # "you are about to destroy 412 bookings" from the export itself rather than
    # re-reading a D1 that is about to be deleted.
    table_counts: dict[str, int] = Field(default_factory=dict)
    lead_count: int = 0

    # ── Retention ────────────────────────────────────────────────────────
    # We are storing a customer's data after they asked us to delete their site,
    # which is only defensible for a bounded window and only because they need a
    # chance to download it. The sweeper purges on this stamp. None means "not yet
    # scheduled", which only happens between the row being minted and the build
    # finishing — never on a ``ready`` row.
    expires_at: datetime | None = None

    class Settings:
        name = "site_exports"
        indexes = [
            # The workspace's exports, newest first — the list read.
            [("workspace", 1), ("createdAt", -1)],
            # The sweeper's scan: everything past its retention stamp.
            [("expires_at", 1)],
        ]
