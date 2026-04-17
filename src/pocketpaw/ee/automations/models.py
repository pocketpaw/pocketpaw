# Automations models — Pydantic models for rule-based pocket automations.
# Created: 2026-03-30 — RuleType enum, Rule, CreateRuleRequest, UpdateRuleRequest.
# Updated: 2026-03-30 — Added ExecutionMode enum, mode/cooldown_minutes/last_evaluated/
#   linked_intention_id fields to Rule, and mode/cooldown_minutes to request models.

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RuleType(StrEnum):
    THRESHOLD = "threshold"
    SCHEDULE = "schedule"
    DATA_CHANGE = "data_change"


class ExecutionMode(StrEnum):
    REQUIRE_APPROVAL = "require_approval"
    AUTO_EXECUTE = "auto_execute"
    NOTIFY_ONLY = "notify_only"


class Rule(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    pocket_id: str = ""
    name: str
    description: str = ""
    enabled: bool = True
    type: RuleType
    # Condition fields
    object_type: str | None = None  # "Product", "Order", etc.
    property: str | None = None  # "stock", "revenue", etc.
    operator: str | None = None  # "less_than", "greater_than", "equals", "changed"
    value: str | None = None
    schedule: str | None = None  # cron expression or preset
    # Action
    action: str = ""  # what to do when rule fires
    # Execution
    mode: ExecutionMode = ExecutionMode.REQUIRE_APPROVAL
    cooldown_minutes: int = 60  # don't re-fire within this window
    # Stats
    last_fired: datetime | None = None
    last_evaluated: datetime | None = None
    fire_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # Bridge to core daemon
    linked_intention_id: str | None = None  # core daemon intention ID


class CreateRuleRequest(BaseModel):
    pocket_id: str = ""
    name: str
    description: str = ""
    type: RuleType
    object_type: str | None = None
    property: str | None = None
    operator: str | None = None
    value: str | None = None
    schedule: str | None = None
    action: str = ""
    mode: ExecutionMode | None = None
    cooldown_minutes: int | None = None


class UpdateRuleRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    object_type: str | None = None
    property: str | None = None
    operator: str | None = None
    value: str | None = None
    schedule: str | None = None
    action: str | None = None
    mode: ExecutionMode | None = None
    cooldown_minutes: int | None = None
    last_evaluated: datetime | None = None
    linked_intention_id: str | None = None
