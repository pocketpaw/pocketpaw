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
Updated: 2026-07-24 (CX-2, feat/code-agent-exclusive-tools) — added
``AgentConfigSpec.tool_mode`` (``"additive"`` | ``"exclusive"``) mirroring the
Beanie ``AgentConfig.tool_mode``. An ``"exclusive"`` agent's ``tools`` become the
run's MCP allow-list and suppress the universal pocket/widget/atlas grant
(CX-1); ``"additive"`` (the default) is the unchanged legacy grant-union.

Updated: 2026-08-07 (C4-b, feat/coupling-c4b-agentconfig-parity) — added the
``TOOL_MODES`` / ``TOOL_MODE_PATTERN`` constants. The legal set was previously
implicit: ``run_core._agent_tool_policy`` treats ANY value other than
``"exclusive"`` as additive, so a typo'd ``"exlcusive"`` silently opened the tool
surface back up. The request DTOs now validate against these constants, so a bad
value is rejected at the wire boundary instead of fail-opening at run time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pocketpaw_ee.cloud.agents.defaults import CLOUD_DEFAULT_AGENT_BACKEND

#: The legal ``AgentConfigSpec.tool_mode`` values. ``run_core._agent_tool_policy``
#: only branches on ``"exclusive"`` — every other value falls through to the
#: additive grant-union — so an unvalidated typo fails OPEN. Validate against this
#: at every write boundary.
TOOL_MODES: tuple[str, ...] = ("additive", "exclusive")

#: Pydantic ``Field(pattern=...)`` form of ``TOOL_MODES``, derived so the two can
#: never drift apart.
TOOL_MODE_PATTERN: str = f"^({'|'.join(TOOL_MODES)})$"


@dataclass(frozen=True)
class AgentConfigSpec:
    """Configuration data for an agent. Mirrors ``models.agent.AgentConfig``."""

    backend: str = CLOUD_DEFAULT_AGENT_BACKEND
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


__all__ = ["TOOL_MODE_PATTERN", "TOOL_MODES", "Agent", "AgentConfigSpec"]
