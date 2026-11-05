# ee/pocketpaw_ee/site_ai/dto.py — wire shapes for the owner-facing site AI routes.
#
# Pinned by the Sites AI apps contract (docs/design/drafts/2026-11-04-sites-ai-apps-
# tasks.md): ``SiteAiSettings``, ``SiteAiPreset``, ``SiteAiBudget``, ``SiteAiUsage``,
# ``SiteAiModels``. Read-only fields (``plan_models``, ``online_models``,
# ``spent_*``, ``allowance_month_micro``) are in the views only; the PUT bodies never
# accept them. Money is integer micro-credits. ``alias_models`` maps an alias to
# one of the workspace's agents and, optionally, a catalog model id (owner-only
# routes); a site-facing shape never carries either.

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChatAlias = Literal["fast", "smart", "vision"]

MAX_SYSTEM_PROMPT_CHARS = 20_000
MAX_PRESET_NAME_CHARS = 80
# 10 million credits ($100k): far above any real cap, low enough that sums of
# caps never overflow anything.
MAX_CAP_MICRO = 10_000_000 * 1_000_000


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AliasModel(_Body):
    """What serves one alias. ``agent_id`` None = the workspace default agent;
    ``model`` None = that agent's own model. Only the agent's backend and model are
    used, never its prompt, tools or soul."""

    agent_id: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=200)


class SiteAiSettings(BaseModel):
    enabled: bool
    browser_access: bool
    require_preset_for_browser: bool
    allowed_models: list[ChatAlias]
    plan_models: list[str]
    # The plan's aliases that are not offline right now (see aliases.mark_offline).
    online_models: list[str] = Field(default_factory=list)
    alias_models: dict[ChatAlias, AliasModel] = Field(default_factory=dict)
    keep_transcripts: bool


class SiteAiSettingsPut(_Body):
    enabled: bool | None = None
    browser_access: bool | None = None
    require_preset_for_browser: bool | None = None
    allowed_models: list[ChatAlias] | None = Field(default=None, max_length=3)
    # Replaces the whole mapping; an alias left out runs on the default agent.
    alias_models: dict[ChatAlias, AliasModel] | None = None
    keep_transcripts: bool | None = None


class SiteAiAgentOption(BaseModel):
    """One workspace agent an alias can run on (its backend and model only)."""

    id: str
    name: str
    backend: str
    model: str  # the agent's own model, "" = its backend's default
    default: bool  # the workspace default agent (what an unmapped alias uses)


class SiteAiModels(BaseModel):
    """GET .../ai/models: what an owner can map an alias to. ``agents`` are the
    workspace agents the owner may use; ``models`` the chat entries of the same
    catalog the per-send model picker reads (``GET /catalog/models``, with this
    workspace's availability). ``catalog_error`` is true when the catalog source
    was unreachable (``models`` is then empty, agents still listed)."""

    agents: list[SiteAiAgentOption]
    models: list[dict]
    default_agent_id: str | None
    catalog_error: bool = False


class SiteAiPreset(BaseModel):
    id: str
    name: str
    system_prompt: str
    model: ChatAlias
    max_output_tokens: int
    temperature: float
    version: int
    updated_at: str


class SiteAiPresetBody(_Body):
    name: str = Field(min_length=1, max_length=MAX_PRESET_NAME_CHARS)
    system_prompt: str = Field(max_length=MAX_SYSTEM_PROMPT_CHARS)
    model: ChatAlias = "fast"
    max_output_tokens: int = Field(default=1024, ge=1, le=32_000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)


class SiteAiBudget(BaseModel):
    monthly_cap_micro: int
    daily_cap_micro: int
    per_visitor_daily_micro: int
    preview_daily_cap_micro: int
    alert_thresholds: list[int]
    paused: bool
    spent_today_micro: int
    spent_month_micro: int
    allowance_month_micro: int


class SiteAiBudgetPut(_Body):
    monthly_cap_micro: int | None = Field(default=None, ge=0, le=MAX_CAP_MICRO)
    daily_cap_micro: int | None = Field(default=None, ge=0, le=MAX_CAP_MICRO)
    per_visitor_daily_micro: int | None = Field(default=None, ge=0, le=MAX_CAP_MICRO)
    preview_daily_cap_micro: int | None = Field(default=None, ge=0, le=MAX_CAP_MICRO)
    alert_thresholds: list[int] | None = Field(default=None, max_length=5)
    paused: bool | None = None


class UsageBucket(BaseModel):
    micro: int
    calls: int


class UsageDay(UsageBucket):
    day: str


class UsageModel(UsageBucket):
    model: str


class UsageCaller(UsageBucket):
    caller: Literal["worker", "browser", "preview"]


class SiteAiUsage(BaseModel):
    from_: str = Field(serialization_alias="from")
    to: str
    total_micro: int
    calls: int
    by_day: list[UsageDay]
    by_model: list[UsageModel]
    by_caller: list[UsageCaller]

    model_config = ConfigDict(populate_by_name=True)


class SiteAiTranscript(BaseModel):
    run_id: str
    caller: str
    model: str
    created_at: str
    messages: list[dict]
    output: str
    tool_calls: list[dict]


__all__ = [
    "AliasModel",
    "MAX_CAP_MICRO",
    "MAX_PRESET_NAME_CHARS",
    "MAX_SYSTEM_PROMPT_CHARS",
    "ChatAlias",
    "SiteAiBudget",
    "SiteAiBudgetPut",
    "SiteAiAgentOption",
    "SiteAiModels",
    "SiteAiPreset",
    "SiteAiPresetBody",
    "SiteAiSettings",
    "SiteAiSettingsPut",
    "SiteAiTranscript",
    "SiteAiUsage",
    "UsageCaller",
    "UsageDay",
    "UsageModel",
]
