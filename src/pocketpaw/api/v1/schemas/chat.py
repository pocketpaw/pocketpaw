# Chat schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Send a message for processing."""

    content: str = Field(..., min_length=1, max_length=100000)
    session_id: str | None = None
    media: list[str] = []


class ChatChunk(BaseModel):
    """A single SSE event chunk."""

    event: str
    data: dict


class ChatResponse(BaseModel):
    """Complete (non-streaming) chat response."""

    session_id: str
    content: str
    usage: dict = {}
