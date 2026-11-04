# ee/pocketpaw_ee/cloud/models/site_ai.py — the Mongo documents behind a site's AI API.
#
# All keyed by (workspace, pocket_id): a site's settings follow its pocket, so a
# transfer moves them and a delete cascades them. Only ``pocketpaw_ee.site_ai``
# reads or writes these.
#
#   SiteAiSettingsDoc  one per site: on/off, browser access, preset rule, the
#                      model aliases the owner allows, transcripts opt-in.
#   SiteAiPresetDoc    owner-saved system prompt + model + caps. ``system_prompt``
#                      is server-only: never returned to a site caller.
#   SiteAiBudgetDoc    one per site: the caps (micro-credits), alert thresholds,
#                      which thresholds already alerted this period, pause switch.
#   SiteAiCallDoc      one per call: the reservation (held -> settled | released)
#                      and, once settled, the usage row the owner's usage reads
#                      aggregate. Token counts and hashes only, never content.
#   SiteAiCounterDoc   atomic spend counters per (site, scope, period): ``committed``
#                      is held + spent and is the figure every cap is tested on.
#
# Money is integer micro-credits throughout (1 credit = 1,000,000 micro = $0.01).

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from beanie import Document
from pydantic import Field
from pymongo import IndexModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SiteAiSettingsDoc(Document):
    workspace: str
    pocket_id: str
    enabled: bool = True
    browser_access: bool = False
    require_preset_for_browser: bool = False
    allowed_models: list[str] = Field(default_factory=lambda: ["fast"])
    keep_transcripts: bool = False
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "site_ai_settings"
        indexes = [IndexModel([("workspace", 1), ("pocket_id", 1)], unique=True)]


class SiteAiPresetDoc(Document):
    workspace: str
    pocket_id: str
    preset_id: str
    name: str
    system_prompt: str
    model: str = "fast"
    max_output_tokens: int = 1024
    temperature: float = 0.7
    version: int = 1
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "site_ai_presets"
        indexes = [
            IndexModel([("workspace", 1), ("pocket_id", 1), ("preset_id", 1)], unique=True),
        ]


class SiteAiBudgetDoc(Document):
    workspace: str
    pocket_id: str
    monthly_cap_micro: int = 1_000 * 1_000_000
    daily_cap_micro: int = 100 * 1_000_000
    per_visitor_daily_micro: int = 20 * 1_000_000
    preview_daily_cap_micro: int = 50 * 1_000_000
    alert_thresholds: list[int] = Field(default_factory=lambda: [50, 80, 100])
    # period ("2026-11") -> the highest threshold already alerted in it.
    alerted: dict[str, int] = Field(default_factory=dict)
    paused: bool = False
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "site_ai_budgets"
        indexes = [IndexModel([("workspace", 1), ("pocket_id", 1)], unique=True)]


CallState = Literal["held", "settled", "released"]


class SiteAiCallDoc(Document):
    run_id: str
    workspace: str
    pocket_id: str
    site_id: str
    caller: Literal["worker", "browser", "preview"]
    model: str  # the alias, never the provider model id
    kind: Literal["chat", "embed"] = "chat"
    preset_id: str | None = None
    preset_version: int | None = None
    # Counter ids this call holds against (day, month, visitor, preview).
    counters: list[str] = Field(default_factory=list)
    reserved_micro: int = 0
    actual_micro: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    state: CallState = "held"
    day: str = ""  # UTC "YYYY-MM-DD" the call is counted on
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime = Field(default_factory=_utcnow)
    # Set when the request was handed to the provider: from then on the call
    # costs at least its input, so a dead hold settles instead of releasing.
    sent_at: datetime | None = None
    settled_at: datetime | None = None

    class Settings:
        name = "site_ai_calls"
        indexes = [
            IndexModel([("run_id", 1)], unique=True),
            IndexModel([("workspace", 1), ("pocket_id", 1), ("day", 1)]),
            IndexModel([("state", 1), ("expires_at", 1)]),
            # Usage history is read for at most a year back.
            IndexModel([("created_at", 1)], expireAfterSeconds=400 * 24 * 3600),
        ]


class SiteAiCounterDoc(Document):
    # ``key`` is "{workspace}:{pocket_id}:{scope}:{period}", scope one of day,
    # month, visitor:<hash>, preview; or "{workspace}:*:wallet:inflight", the
    # workspace's in-flight holds (held only, never spent: the wallet is the spend).
    key: str
    workspace: str
    pocket_id: str
    committed_micro: int = 0
    held_micro: int = 0
    spent_micro: int = 0
    calls: int = 0
    created_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "site_ai_counters"
        indexes = [
            IndexModel([("key", 1)], unique=True),
            IndexModel([("workspace", 1), ("pocket_id", 1)]),
            # Day and visitor counters are useless after their day; month ones
            # after their month. 40 days covers both.
            IndexModel([("created_at", 1)], expireAfterSeconds=40 * 24 * 3600),
        ]


__all__ = [
    "SiteAiBudgetDoc",
    "SiteAiCallDoc",
    "SiteAiCounterDoc",
    "SiteAiPresetDoc",
    "SiteAiSettingsDoc",
]
