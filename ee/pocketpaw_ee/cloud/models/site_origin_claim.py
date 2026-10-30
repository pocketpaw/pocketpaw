# ee/pocketpaw_ee/cloud/models/site_origin_claim.py — the record that one
# workspace proved it controls one origin (host). Written and read ONLY through
# ``pocketpaw_ee.sites.ownership``.
#
# WHAT IT GATES. A later slice crawls a customer's own pages to ground a
# concierge agent. Without a proof of control that is a crawler-for-hire: anyone
# could name a third party's host and have its content fetched and compiled into
# their workspace. This row is that proof, so its shape carries two invariants:
#
#   * ONE ROW PER (workspace, host), enforced by a unique index. The token lives
#     on the CLAIMANT'S row, so a verification is always a comparison against the
#     token THIS workspace was issued. That is what makes a token unreplayable
#     across workspaces — workspace B publishing A's token finds B's own row and
#     mismatches. Never look a claim up by token alone; that reintroduces the
#     replay this index exists to prevent.
#   * ``token`` IS A SECRET until it is published on the claimed site. Do not put
#     it on a wire shape that is readable by anyone but the issuing workspace,
#     and do not log it.
#
# ``status`` is "pending" (issued, unproven) or "verified". ``expires_at`` bounds
# the PENDING window only — a token older than that will not verify, so an
# abandoned claim cannot be redeemed by whoever controls the host later.
# Verification itself is durable: nothing here expires a proven origin, because
# the re-proof cadence belongs to the consumer that acts on it.

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SiteOriginClaim(TimestampedDocument):
    """One workspace's claim on one origin, pending or proven."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    # Normalized host: lowercase, no port, no trailing dot. Never a URL.
    host: str
    # The secret the owner publishes on their site. Compared constant-time.
    token: str
    status: str = "pending"  # "pending" | "verified"
    issued_at: datetime
    # The PENDING token's deadline. Not a deadline on a verified origin.
    expires_at: datetime
    verified_at: datetime | None = None
    # Which proof carried it: "" (unproven) | "well-known" | "meta".
    method: str = ""
    # The user who asked for the token, for the audit trail.
    issued_by: str = ""

    class Settings:
        name = "site_origin_claims"
        indexes = [
            # One claim per (workspace, host) — the binding the replay guard
            # rests on. A re-claim REPLACES the token on this row.
            IndexModel(
                [("workspace", 1), ("host", 1)],
                unique=True,
                name="uq_workspace_host",
            ),
        ]
