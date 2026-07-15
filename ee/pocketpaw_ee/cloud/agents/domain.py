"""Domain value objects for the agents module.

Pure-Python frozen dataclasses. The Beanie ``AgentConfig`` sub-model
mirrors this domain ``AgentConfigSpec`` field-for-field; the repository
converts. We keep the duplication for now because eliminating the
Beanie sub-model would require touching every caller of
``Agent.config.<field>``.

Updated: 2026-06-28 (feat/aiam-agent-revoke, AW-4) — added ``Agent.disabled``
mirroring the new top-level flag on the Beanie doc, so callers (and the wire
dict) can read whether an agent has been soft-disabled / revoked.

Updated: 2026-07-15 (feat/agent-scoped-discover-fields, ASG-1) — added the
additive presentation fields that mirror the new Beanie fields:
``AgentConfigSpec.welcome_message`` / ``conversation_starters`` / ``voice`` /
``appearance`` and ``Agent.tags``. Lists stay frozen-friendly tuples (matching
``tools`` / ``soul_values``); ``voice`` / ``appearance`` are free-form blobs
kept as dicts (nothing hashes these value objects — only ``!=`` equality is
used, in ``service.update``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class AgentConfigSpec:
    """Configuration data for an agent. Mirrors ``models.agent.AgentConfig``."""

    backend: str = "claude_agent_sdk"
    model: str = ""
    system_prompt: str = ""
    tools: tuple[str, ...] = ()
    trust_level: int = 3
    temperature: float = 0.7
    max_tokens: int = 4096
    scopes: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    soul_enabled: bool = True
    soul_persona: str = ""
    soul_archetype: str = ""
    soul_values: tuple[str, ...] = ()
    soul_ocean: tuple[tuple[str, float], ...] = ()  # frozen-friendly dict
    # Presentation fields (ASG-1) — mirror ``models.agent.AgentConfig``.
    welcome_message: str = ""
    conversation_starters: tuple[str, ...] = ()  # frozen-friendly list
    voice: dict | None = None
    appearance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Agent:
    """An agent configuration in a workspace."""

    id: str
    workspace_id: str
    name: str
    slug: str
    avatar: str
    visibility: str  # private | workspace | public
    owner: str  # user_id
    config: AgentConfigSpec
    created_at: datetime
    updated_at: datetime
    disabled: bool = False  # soft-disable / revoke-everywhere (AW-4)
    tags: tuple[str, ...] = ()  # free-form gallery tags (ASG-1)


__all__ = ["Agent", "AgentConfigSpec"]
