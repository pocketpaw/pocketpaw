# Chat schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
    """Send a message for processing.

    Accepts both snake_case and camelCase keys on the wire so the FE can
    post ``sessionId``/``agentId`` without silently losing the value —
    Pydantic defaults to dropping unknown fields, which previously caused
    every ``sessionId: "websocket_xxx"`` payload to be treated as a brand
    new chat with a freshly generated id.
    """

    model_config = ConfigDict(populate_by_name=True)

    content: str = Field(..., min_length=1, max_length=100000)
    session_id: str | None = Field(default=None, alias="sessionId")
    media: list[str] = []
    file_context: FileContext | None = Field(default=None, alias="fileContext")
    pocket_context: PocketContext | None = Field(default=None, alias="pocketContext")

    # Enterprise overrides (all optional, ignored in community mode)
    agent_id: str | None = Field(default=None, alias="agentId")
    workspace_id: str | None = Field(default=None, alias="workspaceId")
    system_prompt: str | None = Field(default=None, alias="systemPrompt")
    model: str | None = None
    tools: list[str] | None = None
    soul_path: str | None = Field(default=None, alias="soulPath")
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
