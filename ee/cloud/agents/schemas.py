"""Agents domain — Pydantic request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=50)
    avatar: str = ""
    visibility: str = Field(default="private", pattern="^(private|workspace|public)$")
    # Agent config
    backend: str = "claude_agent_sdk"
    model: str = ""
    persona: str = ""
    # Optional overrides
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[str] | None = None
    trust_level: int | None = None
    system_prompt: str = ""
    # Soul customization
    soul_enabled: bool = True
    soul_archetype: str = ""
    soul_values: list[str] | None = None
    soul_ocean: dict[str, float] | None = None


class UpdateAgentRequest(BaseModel):
    name: str | None = None
    avatar: str | None = None
    visibility: str | None = Field(default=None, pattern="^(private|workspace|public)$")
    config: dict | None = None
    # Agent config overrides
    backend: str | None = None
    model: str | None = None
    persona: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[str] | None = None
    trust_level: int | None = None
    system_prompt: str | None = None
    # Soul customization
    soul_enabled: bool | None = None
    soul_archetype: str | None = None
    soul_values: list[str] | None = None
    soul_ocean: dict[str, float] | None = None


class DiscoverRequest(BaseModel):
    query: str = ""
    visibility: str | None = None  # filter
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class AgentResponse(BaseModel):
    id: str
    workspace: str
    name: str
    slug: str
    avatar: str
    visibility: str
    config: dict
    owner: str
    created_at: datetime
    updated_at: datetime
