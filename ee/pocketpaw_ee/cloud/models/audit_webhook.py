"""AuditWebhook document — SIEM delivery endpoint for workspace audit events.

Wave 3 Task 15: external HTTPS endpoint that receives a POST per audit
event, signed with HMAC-SHA256. The signing secret must be reversible
(we need the raw bytes to compute the HMAC on each delivery) so it's
stored Fernet-encrypted at rest rather than hashed. Admins see the
plaintext once at create/rotate time; reads return None on the wire.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel


class AuditWebhook(Document):
    workspace: Indexed(str)  # type: ignore[valid-type]
    url: str
    secret: str
    enabled: bool = True
    failure_count: int = 0
    last_delivery_at: datetime | None = None
    last_status: int | None = None
    last_error: str | None = None
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "audit_webhooks"
        indexes = [
            IndexModel([("workspace", 1), ("enabled", 1)]),
        ]
