# ee/pocketpaw_ee/cloud/models/message_log.py — the audit row for one outbound
# /growth delivery attempt (G-5, feat/growth-g5). Workspace-scoped, one row per
# ATTEMPT (not per draft): a send that fails writes ``outcome="failed"`` and the
# retry writes a second row, so the collection is the full delivery history a
# deliverability review or a retry sweep reads.
#
# Only ``ee.cloud.growth.service`` imports this doc class (import-linter
# "Growth" contract) — the dispatch worker records through
# ``service.record_message_log`` (email) and
# ``service.record_whatsapp_attempt`` / ``finish_whatsapp_attempt`` (WhatsApp).
#
# NEVER stores credentials or the provider's auth header — only the
# provider NAME, the provider's own message id, and the recipient address.
# ``error`` carries a connector-produced, credential-free string (see
# ``growth/connector.py``, which raises only sanitised messages).
#
# This is also the COMPLIANCE record, not just a convenience log. Meta's
# business-initiated messaging policy makes "we only messaged opted-in people" a
# claim we have to be able to prove after the fact, per recipient, so the
# WhatsApp path writes a row BEFORE the provider is called and finalises it
# after — including the attempts it REFUSED. ``outcome="blocked"`` +
# ``blocked_reason="not_opted_in"`` is the row that proves the guard fired and
# no message left the building.
#
# Created 2026-07-27 (feat/growth-g5): fifth slice of /growth — email dispatch.
# Updated 2026-07-27 (integration/growth-v1): unified with G-6's
# ``WhatsAppSendLog``. G-5 and G-6 built in parallel and could not see each
# other, so each minted its own send record; this doc is the single survivor.
# It gains the four fields the WhatsApp path needs and G-5's email shape
# lacked: the ``sending`` and ``blocked`` outcomes (a row written before the
# provider call, and a row for a refused attempt), ``blocked_reason``,
# ``error_code``, and ``opted_in_at_attempt`` — the opt-in fact AS OF the
# attempt, so a later edit to the prospect can never rewrite what we knew when
# we pressed send. WhatsApp's ``to_number`` folds into ``to_address``: both are
# "the recipient as resolved at attempt time".

from __future__ import annotations

from datetime import datetime

from beanie import Indexed

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class MessageLog(TimestampedDocument):
    """One outbound delivery attempt for a growth draft, including refusals."""

    # Tenancy boundary — every read filters on this.
    workspace: Indexed(str)  # type: ignore[valid-type]
    # The growth_drafts row this attempt delivered (stringified ObjectId).
    draft_id: str
    # The growth_prospects row the draft targets (stringified ObjectId).
    prospect_id: str
    channel: str  # email | linkedin | whatsapp
    provider: str  # "mailtrap" for email, "msg91" for whatsapp
    provider_message_id: str | None = None  # provider's own id, when it returns one
    # The recipient, as it was resolved at attempt time. Kept rather than
    # re-derived because the compliance question is always "who did you message,
    # and were they opted in", and the prospect row can be edited afterwards.
    to_address: str
    sent_at: datetime | None = None  # set on a successful send only
    # ``sending`` is written BEFORE the provider call and finalised to ``sent``
    # or ``failed`` after it (the WhatsApp path, which needs an in-flight trace
    # and a rate-cap window that counts committed attempts). ``blocked`` means a
    # guard refused the attempt and NO provider call was made at all. The email
    # path writes ``sent`` / ``failed`` directly.
    outcome: str  # sending | sent | failed | blocked
    # Machine-readable reason a ``blocked`` row exists (``not_opted_in``,
    # ``rate_capped``, ``draft_not_approved``, ``no_number``, ``not_configured``).
    blocked_reason: str = ""
    error_code: str = ""
    error: str | None = None  # sanitised failure detail; never a credential
    # The opt-in fact AS OF the attempt — a later edit to the prospect can never
    # rewrite what we knew when we pressed send.
    opted_in_at_attempt: bool = False

    class Settings:
        name = "growth_message_logs"
        indexes = [
            # A draft's delivery history — the retry sweep and the "did this
            # actually go out?" lookup.
            [("workspace", 1), ("draft_id", 1)],
            # Failure triage / deliverability review per channel.
            [("workspace", 1), ("outcome", 1), ("channel", 1)],
            # The WhatsApp rate-cap window scan: attempts on one channel that
            # reached the provider in this workspace within the trailing hour.
            [("workspace", 1), ("channel", 1), ("outcome", 1), ("createdAt", -1)],
            # The compliance query: everything we ever sent to one recipient.
            [("workspace", 1), ("to_address", 1)],
        ]
