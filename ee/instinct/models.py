# Instinct data models — decision pipeline types.
# Created: 2026-03-28

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from ee.fabric.models import _gen_id


class ActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


class ActionPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionCategory(StrEnum):
    DATA = "data"
    ALERT = "alert"
    WORKFLOW = "workflow"
    CONFIG = "config"
    EXTERNAL = "external"


class ActionTrigger(BaseModel):
    """What triggered an action."""

    type: str  # "agent", "automation", "user", "connector"
    source: str  # agent name, rule ID, user ID, connector name
    reason: str


class ActionContext(BaseModel):
    """Data context for a decision."""

    object_ids: list[str] = Field(default_factory=list)
    connector_data: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    notes: str = ""


class Action(BaseModel):
    """A proposed action from the agent, waiting for approval."""

    id: str = Field(default_factory=lambda: _gen_id("act"))
    pocket_id: str
    title: str
    description: str
    category: ActionCategory = ActionCategory.WORKFLOW
    status: ActionStatus = ActionStatus.PENDING
    priority: ActionPriority = ActionPriority.MEDIUM
    trigger: ActionTrigger
    recommendation: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    context: ActionContext = Field(default_factory=ActionContext)
    outcome: str | None = None
    error: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejected_reason: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    executed_at: datetime | None = None


class AuditCategory(StrEnum):
    DECISION = "decision"
    DATA = "data"
    CONFIG = "config"
    SECURITY = "security"


class AuditEntry(BaseModel):
    """An audit log entry for every decision."""

    id: str = Field(default_factory=lambda: _gen_id("aud"))
    action_id: str | None = None
    pocket_id: str | None = None
    timestamp: datetime = Field(default_factory=datetime.now)
    actor: str  # "agent:claude", "user:prakash", "system"
    event: str  # "action_proposed", "action_approved", etc.
    category: AuditCategory = AuditCategory.DECISION
    description: str
    context: dict[str, Any] = Field(default_factory=dict)
    ai_recommendation: str | None = None
    outcome: str | None = None
