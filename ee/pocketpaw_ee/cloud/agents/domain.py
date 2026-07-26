"""Domain value objects for the agents module.

Pure-Python frozen dataclasses. The Beanie ``AgentConfig`` sub-model
mirrors this domain ``AgentConfigSpec`` field-for-field; the repository
converts. We keep the duplication for now because eliminating the
Beanie sub-model would require touching every caller of
``Agent.config.<field>``.

Updated: 2026-06-28 (feat/aiam-agent-revoke, AW-4) — added ``Agent.disabled``
mirroring the new top-level flag on the Beanie doc, so callers (and the wire
dict) can read whether an agent has been soft-disabled / revoked.
Updated: 2026-07-24 (CX-2, feat/code-agent-exclusive-tools) — added
``AgentConfigSpec.tool_mode`` (``"additive"`` | ``"exclusive"``) mirroring the
Beanie ``AgentConfig.tool_mode``. An ``"exclusive"`` agent's ``tools`` become the
run's MCP allow-list and suppress the universal pocket/widget/atlas grant
(CX-1); ``"additive"`` (the default) is the unchanged legacy grant-union.
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
    # Tool-surface policy. "additive" (default) UNIONs the agent's tools with the
    # universal MCP grant (legacy). "exclusive" caps the run's MCP surface to
    # exactly ``tools`` (CX-1/CX-2) — a non-empty ``tools`` list alone does NOT
    # imply exclusive; only this flag does.
    tool_mode: str = "additive"
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


__all__ = ["Agent", "AgentConfigSpec"]
