# ee/cloud/mission_control/domain.py
# Created: 2026-05-13 (feat/mission-control-facade) — WorkItem value object
# projecting Instinct's Action shape (and, later, the Tasks entity from PR 2)
# into the unified frontend representation. Frozen dataclass per the ee/cloud
# Code Rules (CLAUDE.md §3): workspace + assignee tenancy fields are required,
# so constructing a WorkItem without them is a type error.
"""Mission Control domain value objects.

Pure-Python frozen dataclasses. No Beanie, no Pydantic, no FastAPI
imports. The service maps from Instinct's ``Action`` (and other source
shapes) into these; the DTO layer maps these into the wire response.

Why this shape: the paw-enterprise mock UI consumes a single canonical
``WorkItem`` for The Tray, Pawprints, Snags, and Agents-in-flight panes.
That keeps the frontend store small. The audit doc
(`docs/internal/2026-05-mission-control-backend-audit.md`) commits to it
as the boundary between heterogeneous backend primitives and the
homogeneous frontend feed.

Tenancy:
  - ``workspace_id`` is the active workspace. The façade service refuses
    to construct a WorkItem without it. The pocket service already
    enforces workspace scoping on its reads, so the façade can rely on
    ``pockets_service.list_pockets(workspace_id, user_id)`` to gate which
    Instinct actions are visible.
  - ``assignee_kind`` / ``assignee_id`` mirror the polymorphic
    assignment Tasks (PR 2) will carry. For PR 1 every Nudge surfaces as
    ``assignee_kind="user"`` (Instinct is human-approval-driven).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class WorkItemSection(StrEnum):
    """The Mission Control pane a WorkItem belongs in."""

    TRAY = "tray"  # awaiting approval — Instinct pending action
    PAWPRINTS = "pawprints"  # approved / rejected — Instinct audit projection
    SNAGS = "snags"  # blocked / failed
    AGENTS = "agents"  # currently in-flight (Tasks, PR 2)


class WorkItemStatus(StrEnum):
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"


class AssigneeKind(StrEnum):
    USER = "user"
    AGENT = "agent"


@dataclass(frozen=True)
class WorkItem:
    """Unified Mission Control work item.

    The wire response (``WorkItemResponse``) is a 1:1 projection of these
    fields — they're separated so callers that don't speak Pydantic
    (CLI, jobs, bus handlers) can still construct + read them.
    """

    id: str
    workspace_id: str
    section: WorkItemSection
    status: WorkItemStatus
    title: str
    description: str
    assignee_kind: AssigneeKind
    assignee_id: str
    pocket_id: str | None
    agent_id: str | None
    source_kind: str  # 'nudge' (PR 1) | 'task' (PR 2) | 'cycle' (PR 3)
    source_id: str
    priority: str  # low | medium | high | critical
    created_at: datetime | None
    updated_at: datetime | None
    fabric_refs: tuple[str, ...] = field(default_factory=tuple)


__all__ = [
    "AssigneeKind",
    "WorkItem",
    "WorkItemSection",
    "WorkItemStatus",
]
