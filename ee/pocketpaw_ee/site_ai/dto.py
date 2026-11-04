# ee/pocketpaw_ee/site_ai/dto.py — wire shapes for the owner-facing site AI routes.
#
# Pinned by the Sites AI apps contract (docs/design/drafts/2026-11-04-sites-ai-apps-
# tasks.md): ``SiteAiSettings``, ``SiteAiPreset``, ``SiteAiBudget``, ``SiteAiUsage``.
# Read-only fields (``plan_models``, ``spent_*``, ``allowance_month_micro``) are in
# the views only; the PUT bodies never accept them. Money is integer micro-credits.
# Model aliases only; a provider model id never appears in any shape here.

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


class SiteAiSettings(BaseModel):
    enabled: bool
    browser_access: bool
    require_preset_for_browser: bool
    allowed_models: list[ChatAlias]
    plan_models: list[str]
    keep_transcripts: bool


class SiteAiSettingsPut(_Body):
    enabled: bool | None = None
    browser_access: bool | None = None
    require_preset_for_browser: bool | None = None
    allowed_models: list[ChatAlias] | None = Field(default=None, max_length=3)
    keep_transcripts: bool | None = None


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
    "MAX_CAP_MICRO",
    "MAX_PRESET_NAME_CHARS",
    "MAX_SYSTEM_PROMPT_CHARS",
    "ChatAlias",
    "SiteAiBudget",
    "SiteAiBudgetPut",
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
