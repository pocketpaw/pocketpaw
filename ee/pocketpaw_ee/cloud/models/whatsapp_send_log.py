# ee/pocketpaw_ee/cloud/models/whatsapp_send_log.py — the per-attempt outbound
# WhatsApp send record for the /growth engine (G-6, feat/growth-g6).
#
# This is the COMPLIANCE record, not a convenience log. Meta's business-initiated
# messaging policy makes "we only messaged opted-in people" a claim we have to be
# able to prove after the fact, per recipient, so every attempt writes a row
# BEFORE the provider is called and the row is finalised after — including the
# attempts we refused. ``status="blocked"`` + ``blocked_reason="not_opted_in"``
# is the row that proves the guard fired and no message left the building.
#
# Named ``WhatsAppSendLog`` deliberately: the sibling G-5 (email dispatch) slice
# introduces its own ``MessageLog``-style record for the email channel. Rather
# than guess at that shape from a concurrent branch, the WhatsApp channel carries
# its own distinctly-named record; reconciling the two into one send log is a
# follow-up once both have landed.
#
# Only ``ee.cloud.growth.service`` imports this doc class (import-linter "Growth"
# contract) — the whatsapp/msg91/webhook modules go through the service.
#
# Created 2026-07-27 (feat/growth-g6): sixth slice of /growth — MSG91 WhatsApp
# dispatch with hard opt-in enforcement.

from __future__ import annotations

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class WhatsAppSendLog(TimestampedDocument):
    """One outbound WhatsApp send attempt (including refused attempts)."""

    # Tenancy boundary — every read filters on this.
    workspace: Indexed(str)  # type: ignore[valid-type]
    draft_id: str
    prospect_id: str
    # The recipient, as it was resolved at attempt time. Kept because the
    # compliance question is always "who did you message, and were they opted
    # in", and the prospect row can be edited afterwards.
    to_number: str = ""
    # ``sending`` is written BEFORE the provider call and finalised to ``sent``
    # or ``failed`` after it. ``blocked`` means a guard refused the attempt and
    # NO provider call was made at all.
    status: str = "sending"  # sending | sent | failed | blocked
    # Machine-readable reason a ``blocked`` row exists (``not_opted_in``,
    # ``rate_capped``, ``draft_not_approved``, ``no_number``, ``not_configured``).
    blocked_reason: str = ""
    provider: str = "msg91"
    provider_message_id: str = ""
    error_code: str = ""
    error: str = ""
    # The opt-in fact AS OF the attempt — a later edit to the prospect can never
    # rewrite what we knew when we pressed send.
    opted_in_at_attempt: bool = False

    class Settings:
        name = "growth_whatsapp_send_logs"
        indexes = [
            # The rate-cap window scan: attempts that reached the provider in
            # this workspace within the trailing hour.
            [("workspace", 1), ("status", 1), ("createdAt", -1)],
            # A draft's send history — the per-draft audit view.
            [("workspace", 1), ("draft_id", 1)],
            # The compliance query: everything we ever sent to one number.
            [("workspace", 1), ("to_number", 1)],
        ]
