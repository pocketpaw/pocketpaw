"""Domain value objects for the agents module.

Pure-Python frozen dataclasses. The Beanie ``AgentConfig`` sub-model
mirrors this domain ``AgentConfigSpec`` field-for-field; the repository
converts. We keep the duplication for now because eliminating the
Beanie sub-model would require touching every caller of
``Agent.config.<field>``.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    soul_enabled: bool = True
    soul_persona: str = ""
    soul_archetype: str = ""
    soul_values: tuple[str, ...] = ()
    soul_ocean: tuple[tuple[str, float], ...] = ()  # frozen-friendly dict


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


__all__ = ["Agent", "AgentConfigSpec"]
