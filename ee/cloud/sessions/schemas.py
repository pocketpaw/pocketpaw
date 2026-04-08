"""Sessions domain — Pydantic request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    title: str = "New Chat"
    pocket_id: str | None = None  # Link to pocket on creation
    group_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None  # Link to existing runtime session (e.g. "websocket_abc123")


class UpdateSessionRequest(BaseModel):
    title: str | None = None
    pocket_id: str | None = None  # Can link/unlink pocket


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class SessionResponse(BaseModel):
    id: str
    session_id: str  # The unique sessionId
    workspace: str
    owner: str
    title: str
    pocket: str | None
    group: str | None
    agent: str | None
    message_count: int
    last_activity: datetime
    created_at: datetime
    deleted_at: datetime | None = None
