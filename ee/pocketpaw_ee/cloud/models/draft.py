# ee/pocketpaw_ee/cloud/models/draft.py — per-channel outreach draft for the
# /growth surface. Workspace-scoped, attached to a ``growth_prospects`` row by
# ``prospect_id``, and to a brand (Growth › Social ``SocialProfile``) by the
# nullable ``profile_id`` (inherited from the prospect when not given). Status
# lifecycle (draft→proposed→approved→sent, sent→replied, non-terminal→rejected)
# is enforced in ``ee.cloud.growth.service`` — the only module allowed to
# import this doc class (import-linter "Growth" contract). Instinct-gated
# proposals and dispatch build on this record. ``blueprint_version`` is the
# approved Brand Blueprint version the writer followed (None for hand-typed and
# templated follow-up drafts, and when the brand had none).

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class Draft(TimestampedDocument):
    """One channel's outreach draft for a prospect in a workspace."""

    # Tenancy boundary — every read filters on this.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The growth_prospects row this copy targets (stringified ObjectId).
    prospect_id: str
    # The brand this copy is written under, or None on a pre-brand row.
    profile_id: str | None = None
    channel: str  # email | linkedin | whatsapp (validated at the DTO boundary)
    subject: str | None = None  # email only
    body: str
    variant: str = "first_touch"  # first_touch | follow_up
    status: str = "draft"  # draft | proposed | approved | sent | replied | rejected
    demo_url: str | None = None
    # The approved Blueprint version the agent followed; None when none was used.
    blueprint_version: int | None = None

    class Settings:
        name = "growth_drafts"
        indexes = [
            # A prospect's drafts — the per-prospect drafts view / dedupe scan.
            [("workspace", 1), ("prospect_id", 1)],
            # The send-gate queue view (G-4): drafts by status per channel.
            [("workspace", 1), ("status", 1), ("channel", 1)],
            # Brand-scoped list and queues.
            [("workspace", 1), ("profile_id", 1)],
        ]
