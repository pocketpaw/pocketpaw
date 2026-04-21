"""Agents domain — Pydantic request/response schemas.

Updated 2026-04-19 (feat/cluster-d-agent-scope-picker): added optional
``scopes: list[str]`` to CreateAgentRequest, UpdateAgentRequest, and a
new ScopeAssignmentRequest body for the dedicated
``PATCH /agents/{id}/scope`` endpoint + ScopeAssignmentResponse for the
matching GET. Scope strings are always re-normalised server-side via the
validator below so a caller cannot bypass the frontend ScopePicker's
normaliseScope guards.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from ee.cloud.agents.scope_rules import normalise_and_validate_scopes

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
    # Scope assignment (hierarchical tags like ``org:sales:*``). Optional;
    # stored on the agent config. Validated server-side — frontend is not
    # trusted as the sole sanitiser.
    scopes: list[str] | None = None
    # Soul customization
    soul_enabled: bool = True
    soul_archetype: str = ""
    soul_values: list[str] | None = None
    soul_ocean: dict[str, float] | None = None

    @field_validator("scopes")
    @classmethod
    def _clean_scopes(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return normalise_and_validate_scopes(v)


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
    scopes: list[str] | None = None
    # Soul customization
    soul_enabled: bool | None = None
    soul_archetype: str | None = None
    soul_values: list[str] | None = None
    soul_ocean: dict[str, float] | None = None

    @field_validator("scopes")
    @classmethod
    def _clean_scopes(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return normalise_and_validate_scopes(v)


class ScopeAssignmentRequest(BaseModel):
    """Body for ``PATCH /agents/{id}/scope``. Always a full replacement —
    the endpoint swaps the stored list rather than merging deltas, so the
    UI and API share a single "these are the scopes now" semantic.
    """

    scopes: list[str]

    @field_validator("scopes")
    @classmethod
    def _clean_scopes(cls, v: list[str]) -> list[str]:
        return normalise_and_validate_scopes(v)


class ScopeAssignmentResponse(BaseModel):
    """Body for ``GET /agents/{id}/scope``. Small, dedicated envelope so
    the UI can cheaply poll scope without pulling the full agent document
    each time.
    """

    agent_id: str
    scopes: list[str]


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
