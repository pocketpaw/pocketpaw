"""AuditEvent document — persistent audit log of workspace mutations.

Wave 2 Task 10: structured audit-log for workspace CRUD, member ops, and
invite ops. Distinct from the legacy ``pocketpaw.audit.store`` SQLite
sink (which captures pocket / skills / agent decisions) — this doc
captures *workspace-governance* writes for the admin-visible audit
surface.

The 1-year TTL keeps the collection bounded; ``workspace + at`` is the
primary read path for cursor-paginated listing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import Field
from pymongo import IndexModel


class AuditEvent(Document):
    workspace: Indexed(str)  # type: ignore[valid-type]
    actor_id: Indexed(str)  # type: ignore[valid-type]
    action: Indexed(str)  # type: ignore[valid-type]
    target_type: str
    target_id: str | None = None
    metadata: dict = Field(default_factory=dict)
    ip: str | None = None
    user_agent: str | None = None
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "audit_events"
        indexes = [
            IndexModel([("workspace", 1), ("at", -1)]),
            IndexModel("at", expireAfterSeconds=86400 * 365),
        ]
