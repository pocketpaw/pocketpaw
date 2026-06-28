"""RequestLog document — dedicated collection for HTTP request/response logs.

Separate from the workspace audit (``AuditEvent``) so API traffic doesn't
pollute the Activity feed. Each row records one HTTP request handled by
the FastAPI application — method, path template, status code, duration,
actor, IP, user-agent.

Indexed on ``(workspace, at)`` for the primary cursor-paginated read path.
A TTL index evicts entries after 30 days by default.
"""

from __future__ import annotations

from datetime import UTC, datetime

from beanie import Document
from pydantic import Field
from pymongo import IndexModel


class RequestLog(Document):
    workspace: str = ""  # empty for non-workspace-scoped endpoints
    # Compound index: (workspace, at) for cursor-paginated listing.
    # A separate index on just "at" drives the 30-day TTL.
    actor_id: str
    method: str
    path: str
    status_code: int
    duration_ms: float
    is_error: bool
    ip: str | None = None
    user_agent: str | None = None
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "request_logs"
        indexes = [
            IndexModel([("workspace", 1), ("at", -1)]),
            IndexModel("at", expireAfterSeconds=86400 * 30),
        ]
