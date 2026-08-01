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

Updated: 2026-08-02 (Sense Phase 2, SP2-5 — the config surface) — the two sense
fields SP2-2/SP2-3 put on the model become OPERABLE over HTTP. Three changes,
each load-bearing:

  * ``CreateAgentRequest`` / ``UpdateAgentRequest`` gained explicit ``senses`` /
    ``sense_prefs`` fields, so they are settable at create (the nested ``config``
    dict does not exist on create at all) and settable on update without having
    to send a whole ``config`` object.
  * ``_config_to_dict`` emits both, so ``GET /agents/{id}`` actually SHOWS what
    an agent carries. Without this the fields were write-only — persisted,
    honoured at run time, invisible to the owner who set them.
  * Sense ids are validated HERE as well as at the Beanie boundary, on the
    explicit fields AND inside the ``config`` dict. The schema validator alone
    raises a pydantic ``ValidationError`` from deep inside the service, which the
    ``CloudError`` handler does not catch — it surfaces as a 500. Validating at
    the DTO makes a bogus id a 422 carrying the vocabulary's own message, which
    is what ``docs/api-reference.md`` has always promised.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from pocketpaw.senses import validate_sense_id
from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.agents.domain import Agent, AgentConfigSpec
from pocketpaw_ee.cloud.agents.scope_rules import normalise_and_validate_scopes

# ---------------------------------------------------------------------------
# Sense-id validation at the wire boundary
# ---------------------------------------------------------------------------


def _check_sense_ids(sense_ids: Any) -> None:
    """Validate every id in an iterable of sense ids (a list, or a dict's keys).

    ``validate_sense_id`` raises ``SenseValidationError`` (a ``ValueError``), so
    pydantic turns it into a field error and FastAPI answers 422 with the
    vocabulary's own message ("unknown core sense id ... the paw.* namespace is
    closed. Known core senses: [...]") rather than a bare "invalid".
    """
    for sense_id in sense_ids:
        validate_sense_id(sense_id)


def _validate_config_sense_fields(config: dict | None) -> dict | None:
    """Validate the sense fields carried inside an update's nested ``config``.

    The ``config``-dict branch of ``service._apply_update`` writes ``senses`` /
    ``sense_prefs`` straight through, so without this the only validation left is
    the Beanie one — a 500 instead of a 422. Non-list / non-dict values are left
    alone: the service's own ``.get`` fallbacks and the Beanie schema decide those.
    """
    if config is None:
        return config
    senses = config.get("senses")
    if isinstance(senses, list):
        _check_sense_ids(senses)
    prefs = config.get("sense_prefs")
    if isinstance(prefs, dict):
        _check_sense_ids(prefs)
    return config


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
    # Sense fields (SP2-5). ``None`` == "leave at the model default" (empty), so
    # an old client that never sends them creates the same agent as before.
    senses: list[str] | None = None
    sense_prefs: dict[str, str] | None = None

    @field_validator("scopes")
    @classmethod
    def _clean_scopes(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else normalise_and_validate_scopes(v)

    @field_validator("senses")
    @classmethod
    def _clean_senses(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            _check_sense_ids(v)
        return v

    @field_validator("sense_prefs")
    @classmethod
    def _clean_sense_prefs(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        # KEYS only — a connector name is workspace state, not schema state
        # (same rule as ``models.agent.AgentConfig``).
        if v is not None:
            _check_sense_ids(v)
        return v


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
    # Sense fields (SP2-5) — ``None`` == "leave unchanged", like every other
    # explicit field here. Settable this way OR inside ``config``; both branches
    # of ``service._apply_update`` carry them.
    senses: list[str] | None = None
    sense_prefs: dict[str, str] | None = None

    @field_validator("scopes")
    @classmethod
    def _clean_scopes(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else normalise_and_validate_scopes(v)

    @field_validator("senses")
    @classmethod
    def _clean_senses(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            _check_sense_ids(v)
        return v

    @field_validator("sense_prefs")
    @classmethod
    def _clean_sense_prefs(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is not None:
            _check_sense_ids(v)
        return v

    @field_validator("config")
    @classmethod
    def _clean_config_senses(cls, v: dict | None) -> dict | None:
        return _validate_config_sense_fields(v)


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
        # SP2-5 — read them back. Persisted + honoured at run time since SP2-2/3,
        # but until they were emitted here the owner had no way to SEE what their
        # agent carries, which makes an exclusive mount list unauditable.
        "senses": list(cfg.senses),
        "sense_prefs": dict(cfg.sense_prefs),
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
