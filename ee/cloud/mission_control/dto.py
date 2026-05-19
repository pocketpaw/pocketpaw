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
# Updated: 2026-05-17 (feat/planner-gaps-and-deps) — pocketpaw#1118 P4
# added ``blocked_by: list[str]`` to ``WorkItemResponse`` and threaded it
# through ``work_item_to_response`` so the frontend feed shows dependency
# edges in the unified WorkItem shape.
# Updated: 2026-05-18 (feat/mc-plan-sessions-endpoint) — added
# ``ListPlanSessionsRequest``, ``PlanSessionDTO`` and
# ``PlanSessionListResponse`` for the new
# ``GET /mission-control/plan-sessions`` endpoint. Status values on the
# wire use the operator vocabulary (``draft``/``active``/``archived``);
# the service maps to the doc-level ``ready``/``stale`` storage form so
# the frontend never has to learn the planner's internal terms.
# Updated: 2026-05-19 (feat/mc-create-cycle-endpoint) — added
# ``CreateCycleRequest`` for the new POST /mission-control/cycles
# endpoint that backs the rail's "+ New cycle" button. Wire format takes
# ISO-8601 strings for ``start`` / ``end`` (date or datetime) so the
# frontend's native ``<input type="date">`` value can post directly
# without coercion. The service derives ``status`` from the dates
# relative to ``now`` (``upcoming`` vs ``active``); ``completed`` is set
# by a separate workflow.
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

    ``project_id`` narrows the feed to one Mission Control Project. The
    Tasks half of the projection threads ``project_id`` directly through
    ``tasks.service.agent_list_tasks``; the Instinct half (Nudges) is
    project-aware via the underlying pocket's ``project_id`` (a Nudge
    inherits its parent pocket's project assignment).
    """

    section: WorkItemSection | None = None
    agent: str | None = Field(default=None, description="Filter by agent id")
    pocket: str | None = Field(default=None, description="Filter by pocket id")
    project_id: str | None = Field(
        default=None,
        description="Filter by project id; empty string narrows to 'Unassigned'.",
    )
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


# Wire-level vocabulary the frontend speaks. The Mission Control Plan tab
# renders "Draft · N tasks" rows; status semantics map to the underlying
# planner doc statuses at the service boundary so the wire stays stable
# even when the doc-level state machine evolves.
PlanSessionStatus = Literal["draft", "active", "archived"]


class ListPlanSessionsRequest(BaseModel):
    """Query filters for ``GET /mission-control/plan-sessions``.

    Both fields are optional. ``status`` narrows to a single Plan-tab
    bucket (drafts vs. active vs. archived); ``limit`` caps the listing
    so a workspace with hundreds of plan sessions doesn't blow up the
    drafts panel.
    """

    status: PlanSessionStatus | None = None
    limit: int = Field(default=50, ge=1, le=200)


class CreateCycleRequest(BaseModel):
    """Body for ``POST /mission-control/cycles``.

    Wire format from the rail's "+ New cycle" form. Mirrors the audit +
    plan-sessions surface conventions: wire stays string-friendly so
    the frontend can post the raw ``<input type="date">`` values without
    coercion, and the service does the parsing + status derivation.

    Fields:
      - ``name`` — operator-supplied label; 1-200 chars per the spec.
      - ``start`` / ``end`` — ISO-8601 date ("2026-05-19") or datetime
        ("2026-05-19T12:00:00Z"). The service parses these and stores
        the date component; ``start`` must be strictly before ``end``.
      - ``project_id`` — optional Mission Control Project the cycle is
        grouped under. When set, the service verifies the project
        belongs to the caller's workspace.
      - ``scope`` — operator's planned-task-count target. Seeds the
        denormalized counter; tasks attaching to the cycle later
        overwrite it from the Tasks collection. ``0`` means unscoped.

    Notes:
      - ``status`` is intentionally absent — the service derives it from
        the parsed dates relative to ``now``. ``completed`` is set via a
        separate close workflow, not by create.
      - ``pocket_id`` isn't in the wire body either — the rail's "+ New
        cycle" button is a workspace-level action; cycles created from
        within a pocket use the cycles entity's own endpoint.
    """

    name: str = Field(min_length=1, max_length=200)
    start: str
    end: str
    project_id: str | None = None
    scope: int = Field(default=0, ge=0)


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
    blocked_by: list[str] = Field(default_factory=list)


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
        blocked_by=list(item.blocked_by),
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


class PlanSessionDTO(BaseModel):
    """Wire shape for one plan session in the drafts list.

    Carries only the metadata the drafts list needs:
      - ``id`` — opaque PlanSession doc id; the frontend round-trips it
        when the operator opens a draft for full detail (separate
        endpoint, not in scope here).
      - ``name`` — display label from the linked Project. Empty string
        when the project was deleted underneath the session.
      - ``status`` — wire vocabulary (``draft``/``active``/``archived``).
        The service maps doc-level ``ready``/``stale`` into this enum.
      - ``task_count`` — number of materialized tasks at plan time.
      - ``created_at`` / ``updated_at`` — ISO-8601 strings rather than
        ``datetime`` objects so the frontend doesn't have to parse a
        Pydantic-serialized timestamp; matches the audit envelope's
        approach.

    The Plan detail (PRD, plan.json, agent gaps) lives behind the
    existing planner endpoints — this DTO intentionally does NOT
    expose the plan content so the drafts list stays cheap.
    """

    id: str
    name: str
    status: PlanSessionStatus
    task_count: int
    created_at: str
    updated_at: str


class PlanSessionListResponse(BaseModel):
    """Envelope for ``GET /mission-control/plan-sessions``.

    Matches the Audit endpoint's ``{ entries, total }`` shape with a
    ``sessions`` key instead of ``entries`` to keep the resource name
    visible. ``total`` is the count of sessions in this response (not
    a workspace-wide total) — the drafts list isn't paginated in v0.
    """

    sessions: list[PlanSessionDTO]
    total: int


__all__ = [
    "ActivityEventResponse",
    "BulkActionRequest",
    "BulkReassignRequest",
    "BulkSnoozeRequest",
    "CreateCycleRequest",
    "ListActivityRequest",
    "ListPlanSessionsRequest",
    "ListWorkItemsRequest",
    "OutcomeSummaryResponse",
    "OutcomesQueryRequest",
    "PlanSessionDTO",
    "PlanSessionListResponse",
    "PlanSessionStatus",
    "WorkItemResponse",
    "work_item_to_response",
]
