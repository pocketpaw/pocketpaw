"""HealthCheckResult dataclass shared by all check modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class HealthCheckResult:
    """Result of a single health check."""

    check_id: str  # e.g. "api_key_primary"
    name: str  # e.g. "Primary API Key"
    category: str  # "config" | "connectivity" | "storage"
    status: str  # "ok" | "warning" | "critical"
    message: str  # e.g. "Anthropic API key is configured"
    fix_hint: str  # e.g. "Set your API key in Settings > API Keys"
    timestamp: str = ""
    details: list[str] | None = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(tz=UTC).isoformat()

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "message": self.message,
            "fix_hint": self.fix_hint,
            "timestamp": self.timestamp,
            "details": self.details,
        }
