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


class PocketContext(BaseModel):
    """Pocket context sent from the desktop client for pocket-scoped chat."""

    id: str
    name: str
    widgets: list[dict] = []
    tool_policy: dict = {}
    model: str | None = None


class ChatRequest(BaseModel):
    """Send a message for processing."""

    content: str = Field(..., min_length=1, max_length=100000)
    session_id: str | None = None
    media: list[str] = []
    file_context: FileContext | None = None
    pocket_context: PocketContext | None = None

    # Enterprise overrides (all optional, ignored in community mode)
    agent_id: str | None = None
    workspace_id: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    soul_path: str | None = None
    channel: str | None = None  # "enterprise" for NestJS requests


class ChatChunk(BaseModel):
    """A single SSE event chunk."""

    event: str
    data: dict


class ChatResponse(BaseModel):
    """Complete (non-streaming) chat response."""

    session_id: str
    content: str
    usage: dict = {}
