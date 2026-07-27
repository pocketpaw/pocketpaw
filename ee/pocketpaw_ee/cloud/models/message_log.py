# ee/pocketpaw_ee/cloud/models/message_log.py — the audit row for one outbound
# /growth delivery attempt (G-5, feat/growth-g5). Workspace-scoped, one row per
# ATTEMPT (not per draft): a send that fails writes ``outcome="failed"`` and the
# retry writes a second row, so the collection is the full delivery history a
# deliverability review or a retry sweep reads.
#
# Only ``ee.cloud.growth.service`` imports this doc class (import-linter
# "Growth" contract) — the dispatch worker records through
# ``service.record_message_log``.
#
# NEVER stores credentials or the provider's auth header — only the
# provider NAME, the provider's own message id, and the recipient address.
# ``error`` carries a connector-produced, credential-free string (see
# ``growth/connector.py``, which raises only sanitised messages).
#
# Created 2026-07-27 (feat/growth-g5): fifth slice of /growth — email dispatch.

from __future__ import annotations

from datetime import datetime

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class MessageLog(TimestampedDocument):
    """One outbound delivery attempt for a growth draft."""

    # Tenancy boundary — every read filters on this.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The growth_drafts row this attempt delivered (stringified ObjectId).
    draft_id: str
    # The growth_prospects row the draft targets (stringified ObjectId).
    prospect_id: str
    channel: str  # email | linkedin | whatsapp
    provider: str  # "mailtrap" for the email channel
    provider_message_id: str | None = None  # provider's own id, when it returns one
    to_address: str
    sent_at: datetime | None = None  # set on a successful send only
    outcome: str  # sent | failed
    error: str | None = None  # sanitised failure detail; never a credential

    class Settings:
        name = "growth_message_logs"
        indexes = [
            # A draft's delivery history — the retry sweep and the "did this
            # actually go out?" lookup.
            [("workspace", 1), ("draft_id", 1)],
            # Failure triage / deliverability review per channel.
            [("workspace", 1), ("outcome", 1), ("channel", 1)],
        ]
