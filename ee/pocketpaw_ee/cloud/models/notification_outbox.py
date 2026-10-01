# ee/pocketpaw_ee/cloud/models/notification_outbox.py
# One external delivery waiting to happen: an email, a signed webhook POST or a
# Slack incoming-webhook POST. ``notifications.outbox`` is the only module that
# reads or writes it. Producers enqueue (``notifications.delivery`` and the
# per-site lead routing); the outbox sweeper claims due rows and sends them.
#
# Lifecycle: ``pending`` -> ``sending`` (claimed, with a ``lease_until``) ->
# ``sent`` | back to ``pending`` with a later ``next_at`` | ``dead``. The claim
# is one atomic ``find_one_and_update``, so two app instances never send the
# same row; a claimant that dies mid-send leaves a lapsed lease, and the row is
# claimable again once ``lease_until`` passes.
#
# ``payload`` never holds a webhook secret or rendered visitor data: a webhook
# row names the config that owns its secret (``webhook_ref``) and a lead row
# carries only the lead id, so the sender loads both at send time.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from beanie import Document
from pydantic import Field
from pymongo import IndexModel


def _now() -> datetime:
    return datetime.now(UTC)


class NotificationOutboxItem(Document):
    workspace: str
    kind: str
    sink: Literal["email", "webhook", "slack"]
    # Email address for ``email``; URL for ``webhook`` / ``slack``.
    target: str
    payload: dict[str, Any] = Field(default_factory=dict)
    # "workspace:<id>" or "site:<site object id>" — whose secret signs a webhook
    # row, and whose failure counter a dead row bumps. "" for email / slack.
    webhook_ref: str = ""
    attempts: int = 0
    next_at: datetime = Field(default_factory=_now)
    status: Literal["pending", "sending", "sent", "dead"] = "pending"
    lease_until: datetime | None = None
    # Minted per claim; finishing a row requires it, so a claimant whose lease
    # lapsed can't overwrite the result of the one that re-claimed the row.
    claim_id: str = ""
    last_error: str | None = None
    created_at: datetime = Field(default_factory=_now)
    # Set when the row reaches ``sent`` or ``dead``; drives the TTL below.
    finished_at: datetime | None = None

    class Settings:
        name = "notification_outbox"
        indexes = [
            # The sweeper's due-row claim: status + next_at (pending rows), and
            # status + lease_until (lapsed ``sending`` leases).
            IndexModel([("status", 1), ("next_at", 1)]),
            IndexModel([("status", 1), ("lease_until", 1)]),
            # Finished rows expire after 30 days; pending/sending rows have no
            # ``finished_at`` and are never reaped by this index.
            IndexModel([("finished_at", 1)], expireAfterSeconds=86400 * 30),
        ]
