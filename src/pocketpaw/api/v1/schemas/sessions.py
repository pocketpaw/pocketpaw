# Session schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class SessionInfo(BaseModel):
    """Session metadata."""

    id: str
    title: str = "Untitled"
    channel: str = "unknown"
    last_activity: str = ""
    message_count: int = 0


class SessionListResponse(BaseModel):
    """Session list response."""

    sessions: list[SessionInfo]
    total: int


class SessionTitleRequest(BaseModel):
    """Session rename request."""

    title: str = Field(..., min_length=1, max_length=200)


class SessionSearchResult(BaseModel):
    """A single search match."""

    id: str
    title: str = "Untitled"
    channel: str = "unknown"
    match: str = ""
    match_role: str = ""
    last_activity: str = ""


class SessionSearchResponse(BaseModel):
    """Session search response."""

    sessions: list[SessionSearchResult]
