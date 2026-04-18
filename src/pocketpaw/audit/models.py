# pocketpaw/audit/models.py — Pydantic models for enterprise audit logging.
# Created: 2026-03-27
# AuditEntry captures who did what, when, why, what data was used,
# what the AI recommended, and what actually happened — for compliance.

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Valid categories for audit entries
AuditCategory = Literal["decision", "data", "config", "security"]

# Valid status values
AuditStatus = Literal["completed", "approved", "rejected", "pending"]


class AuditEntry(BaseModel):
    """A single enterprise audit log entry.

    Captures the full decision context: who acted, on what data,
    what AI recommended, and what actually happened.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    pocket_id: str | None = None
    actor: str  # "agent", "user:prakash", "system"
    action: str  # "create_pocket", "approve_action", "connector_sync"
    category: AuditCategory  # "decision", "data", "config", "security"
    description: str  # Human-readable: "Agent proposed reordering inventory"
    context: dict = Field(default_factory=dict)  # Data used to make the decision
    ai_recommendation: str | None = None  # What the AI suggested
    outcome: str | None = None  # What actually happened
    status: AuditStatus = "completed"
    metadata: dict = Field(default_factory=dict)  # Tool name, connector, etc.

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        valid = {"decision", "data", "config", "security"}
        if v not in valid:
            raise ValueError(f"category must be one of {valid}, got {v!r}")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        valid = {"completed", "approved", "rejected", "pending"}
        if v not in valid:
            raise ValueError(f"status must be one of {valid}, got {v!r}")
        return v

    def to_db_row(self) -> dict:
        """Serialize for SQLite storage."""
        import json

        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "pocket_id": self.pocket_id,
            "actor": self.actor,
            "action": self.action,
            "category": self.category,
            "description": self.description,
            "context": json.dumps(self.context),
            "ai_recommendation": self.ai_recommendation,
            "outcome": self.outcome,
            "status": self.status,
            "metadata": json.dumps(self.metadata),
        }

    @classmethod
    def from_db_row(cls, row: dict) -> AuditEntry:
        """Deserialize from SQLite row."""
        import json

        return cls(
            id=row["id"],
            timestamp=row["timestamp"],
            pocket_id=row["pocket_id"],
            actor=row["actor"],
            action=row["action"],
            category=row["category"],
            description=row["description"],
            context=json.loads(row["context"] or "{}"),
            ai_recommendation=row["ai_recommendation"],
            outcome=row["outcome"],
            status=row["status"],
            metadata=json.loads(row["metadata"] or "{}"),
        )
