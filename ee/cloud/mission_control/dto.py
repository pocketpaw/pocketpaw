# ee/cloud/mission_control/dto.py
# Created: 2026-05-13 (feat/mission-control-facade) — request + response DTOs
# for the Mission Control façade. Request and response shapes are kept on
# separate classes per ee/cloud rule #4 — never reuse a model across input
# and output.
# Updated: 2026-05-13 (feat/mission-control-cleanup) — pinned
# BulkReassignRequest.Assignee.kind to the Tasks vocabulary
# (``Literal["human", "agent"]``) now that the endpoint delegates to
# ``tasks.service.agent_reassign_task`` directly. The Mission Control
# WorkItem domain still uses ``AssigneeKind.USER`` ("user") for Instinct
# projections, but the reassign endpoint only acts on Tasks, so the wire
# contract for the request body follows Tasks.
"""Mission Control wire DTOs.

The audit doc (``docs/internal/2026-05-mission-control-backend-audit.md``,
section "A. ee/cloud/mission_control/") fixes the request + response
shapes. Pydantic validation runs at the router boundary AND at the entry
of every service function (per ee/cloud rule #6), so callers that hit
the service directly (CLI, jobs, bus handlers) get the same input
guarantees as HTTP callers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ee.cloud.mission_control.domain import (
    AssigneeKind,
    WorkItem,
    WorkItemSection,
    WorkItemStatus,
)

# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class ListWorkItemsRequest(BaseModel):
    """Filters for ``GET /mission-control/items``.

    All fields optional. ``section`` filters down to a single pane; the
    other filters compose with it. ``limit`` caps the projected list
    after Instinct fan-in so the response stays bounded even when the
    backend store has thousands of historical rows.
    """

    section: WorkItemSection | None = None
    agent: str | None = Field(default=None, description="Filter by agent id")
    pocket: str | None = Field(default=None, description="Filter by pocket id")
    limit: int = Field(default=50, ge=1, le=500)


class BulkActionRequest(BaseModel):
    """Body for the façade-level bulk endpoints.

    Wraps Instinct's bulk endpoints with a workspace tenancy guard — the
    façade resolves every ``id`` to a pocket and confirms the caller can
    see the pocket before fanning out to Instinct. Ids that fail that
    check come back in ``missing`` rather than raising.
    """

    ids: list[str] = Field(min_length=1)
    note: str | None = None
    reason: str | None = Field(
        default=None,
        description="Required for bulk-reject. Ignored on bulk-approve.",
    )


class BulkReassignRequest(BaseModel):
    """Body for ``POST /mission-control/items/bulk-reassign``.

    Delegates per-id to ``ee.cloud.tasks.service.agent_reassign_task``.
    ``to.kind`` uses the Tasks vocabulary (``human`` / ``agent``) rather
    than the Mission Control display vocabulary (``user`` / ``agent``) so
    the body is forward-routed without translation. Ids that aren't
    Tasks (e.g. ``nudge:<id>``) come back in ``skipped`` — Instinct
    Actions don't carry a polymorphic assignee.
    """

    class Assignee(BaseModel):
        kind: Literal["human", "agent"]
        id: str
        name: str | None = None

    ids: list[str] = Field(min_length=1)
    to: Assignee


class BulkSnoozeRequest(BaseModel):
    """Body for ``POST /mission-control/items/bulk-snooze``.

    Snoozes N Tasks by setting their ``due_at`` to ``until_iso`` via
    ``tasks.service.agent_update_task``. Ids that aren't Tasks come back
    in ``skipped`` — Instinct Actions don't carry a due date.
    """

    ids: list[str] = Field(min_length=1)
    until_iso: str = Field(description="ISO-8601 timestamp to snooze until")


class OutcomesQueryRequest(BaseModel):
    """Query filters for ``GET /mission-control/outcomes``."""

    window: str = Field(
        default="24h",
        description="Aggregation window — 1h, 24h, 7d.",
        pattern=r"^(1h|24h|7d)$",
    )


class ListActivityRequest(BaseModel):
    """Query filters for ``GET /mission-control/activity``."""

    limit: int = Field(default=30, ge=1, le=200)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class WorkItemResponse(BaseModel):
    """Wire shape for one work item — mirrors :class:`WorkItem` 1:1."""

    id: str
    workspace_id: str
    section: WorkItemSection
    status: WorkItemStatus
    title: str
    description: str
    assignee_kind: AssigneeKind
    assignee_id: str
    pocket_id: str | None = None
    agent_id: str | None = None
    source_kind: str
    source_id: str
    priority: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    fabric_refs: list[str] = Field(default_factory=list)


def work_item_to_response(item: WorkItem) -> WorkItemResponse:
    """Map a domain ``WorkItem`` to its wire DTO."""
    return WorkItemResponse(
        id=item.id,
        workspace_id=item.workspace_id,
        section=item.section,
        status=item.status,
        title=item.title,
        description=item.description,
        assignee_kind=item.assignee_kind,
        assignee_id=item.assignee_id,
        pocket_id=item.pocket_id,
        agent_id=item.agent_id,
        source_kind=item.source_kind,
        source_id=item.source_id,
        priority=item.priority,
        created_at=item.created_at,
        updated_at=item.updated_at,
        fabric_refs=list(item.fabric_refs),
    )


class OutcomeSummaryResponse(BaseModel):
    """Aggregated counts for the Outcomes pane.

    Numbers are unchecked against historical drift — they're a live
    snapshot of Instinct's audit table over the requested window. The
    frontend formats them into the headline counters + sparkline.
    """

    window: str
    total: int
    approved: int
    rejected: int
    executed: int
    failed: int
    pending: int


class ActivityEventResponse(BaseModel):
    """Wire shape for an activity ticker entry."""

    workspace_id: str
    kind: str
    agent_id: str | None = None
    summary: str
    pocket_id: str | None = None
    ts: float


__all__ = [
    "ActivityEventResponse",
    "BulkActionRequest",
    "BulkReassignRequest",
    "BulkSnoozeRequest",
    "ListActivityRequest",
    "ListWorkItemsRequest",
    "OutcomeSummaryResponse",
    "OutcomesQueryRequest",
    "WorkItemResponse",
    "work_item_to_response",
]
