from __future__ import annotations

from pydantic import BaseModel, Field


class SessionStatus(BaseModel):
    """Status of a single active agent session."""

    session_key: str
    session_id: str
    channel: str
    title: str | None = None
    state: str  # thinking, tool_running, streaming, waiting_for_user, error
    tool_name: str | None = None
    duration_seconds: float = 0
    token_usage: dict[str, int] | None = None
    error_message: str | None = None


class GlobalStatus(BaseModel):
    """Global agent status."""

    state: str  # idle, active, degraded
    active_sessions: int = 0
    max_concurrent: int = 5
    uptime_seconds: int = 0


class AgentStatusResponse(BaseModel):
    """Full agent status response."""

    global_status: GlobalStatus = Field(alias="global")
    sessions: list[SessionStatus] = []

    model_config = {"populate_by_name": True}
