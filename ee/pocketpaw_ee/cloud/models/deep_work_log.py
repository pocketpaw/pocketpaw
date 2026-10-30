"""DeepWorkLog document — dedicated collection for deep work operations.

Separate from the generic workspace audit (``AuditEvent``) so deep work
activity (cycles, plans, approvals) surfaces cleanly in the Mission Control
Activity tab without mixing with workspace-governance events.

A 365-day TTL keeps the collection bounded.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document
from pydantic import Field
from pymongo import IndexModel


class DeepWorkLog(Document):
    workspace: str
    actor_id: str
    action: str  # e.g. "deep_work.cycle.created", "deep_work.item.approved"
    target_type: str
    target_id: str | None = None
    metadata: dict = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "deep_work_logs"
        indexes = [
            IndexModel([("workspace", 1), ("at", -1)]),
            IndexModel("at", expireAfterSeconds=86400 * 365),
        ]
