# ee/pocketpaw_ee/cloud/models/draft.py — per-channel outreach draft for the
# /growth surface (G-3, feat/growth-g3). Workspace-scoped, attached to a
# ``growth_prospects`` row by ``prospect_id``. Status lifecycle
# (draft→proposed→approved→sent, sent→replied, non-terminal→rejected) is
# enforced in ``ee.cloud.growth.service`` — the only module allowed to import
# this doc class (import-linter "Growth" contract).
#
# Created 2026-07-27 (feat/growth-g3): third slice of /growth — drafts. G-4
# wires Instinct-gated proposals and dispatch on top of this record.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class Draft(TimestampedDocument):
    """One channel's outreach draft for a prospect in a workspace."""

    # Tenancy boundary — every read filters on this.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The growth_prospects row this copy targets (stringified ObjectId).
    prospect_id: str
    channel: str  # email | linkedin | whatsapp (validated at the DTO boundary)
    subject: str | None = None  # email only
    body: str
    variant: str = "first_touch"  # first_touch | follow_up
    status: str = "draft"  # draft | proposed | approved | sent | replied | rejected
    demo_url: str | None = None

    class Settings:
        name = "growth_drafts"
        indexes = [
            # A prospect's drafts — the per-prospect drafts view / dedupe scan.
            [("workspace", 1), ("prospect_id", 1)],
            # The send-gate queue view (G-4): drafts by status per channel.
            [("workspace", 1), ("status", 1), ("channel", 1)],
        ]
