# ee/cloud/models/agent.py — Agent configuration document (not execution).
# Updated 2026-04-19 (feat/cluster-d-agent-scope-picker): added
# ``scopes: list[str]`` to AgentConfig so ScopePicker assignments persist.
# The field is a plain list of hierarchical scope tags (``org:sales:*``)
# validated at the schema boundary via scope_rules.normalise_and_validate.
# Updated 2026-06-08 (feat/agent-plugin-fields, M2): added
# ``skill_refs: list[str]`` and ``plugins: list[str]`` so an agent carries
# its own skill set (direct refs + enabled plugins). These fold into the
# per-run skill materialization (run_core) so they apply on EVERY run the
# agent does, independent of the surface/entity profile.
# Updated 2026-06-28 (feat/aiam-agent-revoke, AW-4): added top-level
# ``disabled: bool = False``. When True the run pool (AgentPool.get) refuses to
# resolve the agent on EVERY path at once — a soft-disable that revokes the
# agent everywhere while letting in-flight runs finish. Top-level (not on
# AgentConfig) so toggling it bumps the doc's ``updatedAt`` and the pool's
# staleness check + explicit cache invalidation both see the change.
# Updated 2026-07-15 (feat/agent-scoped-discover-fields, ASG-1): added additive
# presentation fields for the Agent Gallery / Studio — ``welcome_message``,
# ``conversation_starters``, ``voice``, ``appearance`` on AgentConfig and
# ``tags`` on Agent. All defaulted → zero migration; ``visibility``/``owner``/
# ``workspace`` semantics untouched.
# Updated 2026-07-24 (CX-2, feat/code-agent-exclusive-tools): added
# ``AgentConfig.tool_mode: str = "additive"`` (mirrors the domain
# ``AgentConfigSpec.tool_mode``). "exclusive" makes the agent's ``tools`` the
# run's MCP allow-list and suppresses the universal grant (CX-1); "additive"
# (default) is the unchanged legacy grant-union, so existing agents persist and
# behave exactly as before.
# Updated 2026-08-02 (Sense Phase 2, SP2-2 — agent-tier provider preference):
# added ``AgentConfig.sense_prefs: dict[str, str]`` (sense_id -> connector_name).
# An agent carries its OWN provider choice per sense, and that choice outranks
# the stored pocket/workspace preference rows at resolve time (see
# ``cloud.senses.resolver._disambiguate``). KEYS are validated at the schema
# boundary via ``pocketpaw.senses.validate_sense_id`` — an unknown or malformed
# sense id fails loudly on write. VALUES (connector names) are deliberately NOT
# validated here: a pref naming a connector that isn't currently a candidate for
# the workspace is skipped at resolve time, never an error, so the pref survives
# its provider being temporarily disabled. Defaulted → zero migration.

"""Agent configuration document."""

from __future__ import annotations

from beanie import Indexed
from pydantic import BaseModel, Field, field_validator

from pocketpaw.senses import validate_sense_id
from pocketpaw_ee.cloud.models.base import TimestampedDocument


class AgentConfig(BaseModel):
    backend: str = "claude_agent_sdk"
    model: str = ""  # empty = use backend default
    system_prompt: str = ""
    tools: list[str] = Field(default_factory=list)
    # Tool-surface policy — mirrors the domain ``AgentConfigSpec.tool_mode``.
    # "additive" (default) UNIONs ``tools`` with the universal MCP grant (legacy);
    # "exclusive" caps the run's MCP surface to exactly ``tools`` (CX-1/CX-2).
    tool_mode: str = "additive"
    trust_level: int = Field(default=3, ge=1, le=5)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1)
    # Scope assignment — hierarchical tags that bound the agent's retrieval
    # surface. Empty list == "no scope narrowing" (agent sees whole workspace).
    scopes: list[str] = Field(default_factory=list)
    # Per-agent skill set — folded into the per-run skill materialization so it
    # applies on EVERY run this agent does (any surface). ``skill_refs`` are
    # direct skill names; ``plugins`` are enabled plugin names whose bundled
    # skills resolve via the OSS PluginInstaller registry at run time.
    skill_refs: list[str] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    # Soul integration
    soul_enabled: bool = True
    soul_persona: str = ""
    soul_archetype: str = ""
    soul_values: list[str] = Field(default_factory=lambda: ["helpfulness", "accuracy"])
    soul_ocean: dict[str, float] = Field(
        default_factory=lambda: {
            "openness": 0.7,
            "conscientiousness": 0.85,
            "extraversion": 0.5,
            "agreeableness": 0.8,
            "neuroticism": 0.2,
        }
    )
    # Presentation fields (ASG-1) — surfaced by the Agent Gallery / Studio.
    # All additive + defaulted, so existing docs load unchanged (no migration).
    welcome_message: str = ""
    conversation_starters: list[str] = Field(default_factory=list)
    voice: dict | None = None
    appearance: dict = Field(default_factory=dict)
    # Agent-tier provider preference (SP2-2): sense_id -> connector_name. Wins
    # over the stored pocket/workspace preference rows when the resolver has
    # more than one candidate. Keys are validated below; values are not.
    sense_prefs: dict[str, str] = Field(default_factory=dict)

    @field_validator("sense_prefs")
    @classmethod
    def _validate_sense_pref_keys(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject an unknown / malformed sense id at the schema boundary.

        ``validate_sense_id`` raises ``SenseValidationError`` (a ``ValueError``),
        which pydantic surfaces as a ``ValidationError`` — so a bad key can never
        be persisted. Connector-name values stay unvalidated on purpose: the
        candidate set is workspace state, not schema state.
        """
        for sense_id in value:
            validate_sense_id(sense_id)
        return value


class Agent(TimestampedDocument):
    """Agent configuration (not execution — config only)."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    slug: str
    avatar: str = ""
    config: AgentConfig = Field(default_factory=AgentConfig)
    visibility: str = Field(default="private", pattern="^(private|workspace|public)$")
    owner: str  # User ID
    # Soft-disable / revoke-everywhere (AW-4). True == the run pool refuses to
    # resolve this agent on any NEW request; in-flight runs are unaffected.
    disabled: bool = False
    # Free-form gallery tags (ASG-1). Additive + defaulted → no migration.
    tags: list[str] = Field(default_factory=list)

    class Settings:
        name = "agents"
        indexes = [
            [("workspace", 1), ("slug", 1)],
        ]
