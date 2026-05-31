# Instinct data models — decision pipeline types.
# Created: 2026-03-28
# Updated: 2026-05-21 (feat/instinct-outcome-verification) — issue #1162:
#   Action.outcome can now hold a structured OutcomeVerdict (status +
#   per-criterion results) instead of only a free-text "what happened"
#   string. The field still accepts a plain string for backward
#   compatibility — old executed actions and callers that pass a string
#   keep working. Added OutcomeStatus, CriterionResult, OutcomeVerdict.
# Updated: 2026-05-13 (feat/mission-control-facade) — added optional ``assignee``
#   to Action so The Tray can filter pending items to a specific human approver.

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from pocketpaw.fabric.models import _gen_id


class ActionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


class OutcomeStatus(StrEnum):
    """Verdict on whether an executed action solved the original problem.

    A completed action is an *output*. Whether it solved the problem is an
    *outcome* — and those are not the same thing. This is the verdict half
    of the distinction (issue #1162).
    """

    SOLVED = "solved"  # every success criterion was met
    PARTIAL = "partial"  # some criteria met, some not
    NOT_SOLVED = "not_solved"  # no criteria met
    UNKNOWN = "unknown"  # no criteria were captured — nothing to check


class CriterionResult(BaseModel):
    """Whether one captured success criterion was met by the action result.

    ``criterion`` is the verifiable "this is done when…" statement captured
    at task intake (see deep_work issue #1161). ``met`` is the verifier's
    deterministic verdict for it. ``detail`` carries a short human-readable
    note on how the verdict was reached.
    """

    criterion: str
    met: bool
    detail: str = ""


class OutcomeVerdict(BaseModel):
    """A checked verdict on an executed action — the structured replacement
    for the free-text ``Action.outcome`` string.

    Produced by :func:`pocketpaw.instinct.verification.verify_outcome`. The
    ``status`` summarizes the per-criterion ``criteria_results``; ``summary``
    keeps a free-text "what happened" note so nothing is lost relative to
    the old string-only field.
    """

    status: OutcomeStatus
    criteria_results: list[CriterionResult] = Field(default_factory=list)
    summary: str = ""

    @property
    def met_count(self) -> int:
        """How many success criteria were met."""
        return sum(1 for c in self.criteria_results if c.met)

    @property
    def total_count(self) -> int:
        """How many success criteria were checked."""
        return len(self.criteria_results)


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
    # outcome holds the result of executing the action. Originally a
    # free-text "what happened" string; issue #1162 lets it also be a
    # structured OutcomeVerdict (a checked "did this solve the problem"
    # answer). Both are accepted — a string is the legacy form, still
    # valid. ``mark_executed`` writes whichever the caller passes.
    outcome: str | OutcomeVerdict | None = None
    error: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejected_reason: str | None = None
    assignee: str | None = Field(
        default=None,
        description=(
            "Optional human user id the Nudge is awaiting approval from. "
            "Used by Mission Control's The Tray feed to filter to items "
            "the current operator owns."
        ),
    )
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
