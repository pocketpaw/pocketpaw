"""Wire DTOs for the agents domain.

Replaces ``ee/cloud/agents/schemas.py``. The wire shape uses some
unusual keys preserved from legacy: ``uname`` (the slug), ``createdOn``
and ``lastUpdatedOn`` (with mixedCase). The mappers preserve these
exactly.

Updated: 2026-06-28 (feat/aiam-agent-revoke, AW-4) — ``agent_to_dict`` now
emits ``disabled`` so callers can see whether an agent has been soft-disabled.

Updated: 2026-07-15 (feat/agent-scoped-discover-fields, ASG-1) —
``DiscoverRequest`` gained ``scoped: bool = True`` (the gallery default that
excludes other members' public agents). ``CreateAgentRequest`` /
``UpdateAgentRequest`` accept the additive presentation fields
(``welcome_message`` / ``conversation_starters`` / ``voice`` / ``appearance`` /
``tags``); ``agent_to_dict`` (+ ``_config_to_dict``) and ``AgentResponse`` now
emit them on the wire.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.agents.domain import Agent, AgentConfigSpec
from pocketpaw_ee.cloud.agents.scope_rules import normalise_and_validate_scopes

# ---------------------------------------------------------------------------
# Requests (preserved from schemas.py)
# ---------------------------------------------------------------------------


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=1, max_length=50)
    avatar: str = ""
    visibility: str = Field(default="private", pattern="^(private|workspace|public)$")
    backend: str = "claude_agent_sdk"
    model: str = ""
    persona: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[str] | None = None
    trust_level: int | None = None
    system_prompt: str = ""
    scopes: list[str] | None = None
    skill_refs: list[str] | None = None
    plugins: list[str] | None = None
    soul_enabled: bool = True
    soul_archetype: str = ""
    soul_values: list[str] | None = None
    soul_ocean: dict[str, float] | None = None
    # Presentation fields (ASG-1) — additive; all optional so old clients
    # keep working. ``welcome_message`` defaults to "" like ``system_prompt``.
    welcome_message: str = ""
    conversation_starters: list[str] | None = None
    voice: dict | None = None
    appearance: dict | None = None
    tags: list[str] | None = None

    @field_validator("scopes")
    @classmethod
    def _clean_scopes(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else normalise_and_validate_scopes(v)


class UpdateAgentRequest(BaseModel):
    name: str | None = None
    avatar: str | None = None
    visibility: str | None = Field(default=None, pattern="^(private|workspace|public)$")
    config: dict | None = None
    backend: str | None = None
    model: str | None = None
    persona: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[str] | None = None
    trust_level: int | None = None
    system_prompt: str | None = None
    scopes: list[str] | None = None
    skill_refs: list[str] | None = None
    plugins: list[str] | None = None
    soul_enabled: bool | None = None
    soul_archetype: str | None = None
    soul_values: list[str] | None = None
    soul_ocean: dict[str, float] | None = None
    # Presentation fields (ASG-1) — all optional (None == "leave unchanged").
    welcome_message: str | None = None
    conversation_starters: list[str] | None = None
    voice: dict | None = None
    appearance: dict | None = None
    tags: list[str] | None = None

    @field_validator("scopes")
    @classmethod
    def _clean_scopes(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else normalise_and_validate_scopes(v)


class ScopeAssignmentRequest(BaseModel):
    scopes: list[str]

    @field_validator("scopes")
    @classmethod
    def _clean_scopes(cls, v: list[str]) -> list[str]:
        return normalise_and_validate_scopes(v)


class ScopeAssignmentResponse(BaseModel):
    agent_id: str
    scopes: list[str]


class DiscoverRequest(BaseModel):
    query: str = ""
    visibility: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    # When True (the gallery default) the no-explicit-visibility union excludes
    # other members' public agents — see ``service.discover``. Pass False to
    # restore the legacy cross-workspace public union.
    scoped: bool = True


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _config_to_dict(cfg: AgentConfigSpec) -> dict[str, Any]:
    """Map domain config to the legacy wire-format dict."""
    return {
        "backend": cfg.backend,
        "model": cfg.model,
        "system_prompt": cfg.system_prompt,
        "tools": list(cfg.tools),
        "trust_level": cfg.trust_level,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "scopes": list(cfg.scopes),
        "skill_refs": list(cfg.skill_refs),
        "plugins": list(cfg.plugins),
        "soul_enabled": cfg.soul_enabled,
        "soul_persona": cfg.soul_persona,
        "soul_archetype": cfg.soul_archetype,
        "soul_values": list(cfg.soul_values),
        "soul_ocean": dict(cfg.soul_ocean),
        "welcome_message": cfg.welcome_message,
        "conversation_starters": list(cfg.conversation_starters),
        "voice": cfg.voice,
        "appearance": dict(cfg.appearance),
    }


def agent_to_dict(agent: Agent) -> dict[str, Any]:
    """Map a domain Agent to its legacy wire-format dict.

    Preserves the unusual keys: ``_id``, ``uname`` (slug), ``createdOn``,
    ``lastUpdatedOn`` (mixedCase). Returning a dict directly (rather than
    a Pydantic model) matches what the legacy `_agent_response` produced
    byte-for-byte.
    """
    return {
        "_id": agent.id,
        "workspace": agent.workspace_id,
        "name": agent.name,
        "uname": agent.slug,
        "avatar": agent.avatar,
        "visibility": agent.visibility,
        "config": _config_to_dict(agent.config),
        "owner": agent.owner,
        "disabled": agent.disabled,
        "tags": list(agent.tags),
        "createdOn": iso_utc(agent.created_at),
        "lastUpdatedOn": iso_utc(agent.updated_at),
    }


class AgentResponse(BaseModel):
    """Legacy Pydantic envelope for an agent. The router does NOT use
    this — the wire-format dict from `agent_to_dict` is what callers see.
    Kept for backward compat with `tests/cloud/test_agent_schemas.py`."""

    id: str
    workspace: str
    name: str
    slug: str
    avatar: str
    visibility: str
    config: dict
    owner: str
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


__all__ = [
    "AgentResponse",
    "CreateAgentRequest",
    "DiscoverRequest",
    "ScopeAssignmentRequest",
    "ScopeAssignmentResponse",
    "UpdateAgentRequest",
    "agent_to_dict",
]
