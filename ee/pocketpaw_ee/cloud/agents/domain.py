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
Updated: 2026-08-02 (Sense Phase 2, SP2-2 — agent-tier provider preference) —
added ``AgentConfigSpec.sense_prefs``, the frozen-friendly mirror of the Beanie
``AgentConfig.sense_prefs`` dict (sense_id -> connector_name), carried as a tuple
of pairs like ``soul_ocean``. It mirrors here rather than living doc-only because
``service.update`` round-trips doc -> domain -> doc: a field missing from the
domain spec is silently ERASED the next time any other config field changes.
Sense-id validation stays at the Beanie boundary — this module is pure data.
Updated: 2026-08-02 (Sense Phase 2, SP2-3 — the sense mount list) — added
``AgentConfigSpec.senses``, the frozen-friendly mirror of the Beanie
``AgentConfig.senses`` list. Empty = inherit the workspace's resolvable senses;
non-empty = the EXCLUSIVE set this agent carries. It mirrors here for the same
erasure reason as ``sense_prefs``, plus a second one: the MCP layer builds its
``AgentSenseContext`` from the DOMAIN agent (``service.get``), so a doc-only
field would never reach the resolver at all.
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
    # Sense mount list (SP2-3). Empty = inherit every sense that resolves for
    # the workspace; non-empty = the EXCLUSIVE set the agent carries.
    senses: tuple[str, ...] = ()
    # Agent-tier provider preference (SP2-2). Frozen-friendly dict, same shape
    # as ``soul_ocean``: sense_id -> connector_name. A pref for a sense outside
    # ``senses`` is dead config — the mount gate refuses before the pref is read.
    sense_prefs: tuple[tuple[str, str], ...] = ()


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
