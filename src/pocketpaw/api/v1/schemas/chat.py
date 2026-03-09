# Chat schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class FileContext(BaseModel):
    """Optional file/directory context from the desktop client."""

    current_dir: str | None = None
    open_file: str | None = None
    open_file_name: str | None = None
    open_file_extension: str | None = None
    open_file_size: int | None = None
    selected_files: list[str] | None = None
    source: str | None = None


class ChatRequest(BaseModel):
    """Send a message for processing."""

    content: str = Field(..., min_length=1, max_length=100000)
    session_id: str | None = None
    media: list[str] = []
    file_context: FileContext | None = None


class ChatChunk(BaseModel):
    """A single SSE event chunk."""

    event: str
    data: dict


class ChatResponse(BaseModel):
    """Complete (non-streaming) chat response."""

    session_id: str
    content: str
    usage: dict = {}
