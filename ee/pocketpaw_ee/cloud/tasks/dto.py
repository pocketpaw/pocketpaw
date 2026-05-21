# dto.py — request/response Pydantic schemas for the Tasks entity.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend.
#   Distinct *Request and *Response models; never reuse one model for
#   both input and output (per ee/cloud Code Rules §4). Domain → wire
#   mapper lives at the bottom alongside its tests' surface.
# Updated: 2026-05-17 (feat/planner-gaps-and-deps) — pocketpaw#1118 P4
#   added ``blocked_by`` to CreateTaskRequest, UpdateTaskRequest, and
#   TaskResponse. Update semantics distinguish None (no change) from
#   an explicit empty list (clear dependencies). Domain ↔ wire mapper
#   threads the field through ``task_to_dto``.
"""Tasks entity — request/response DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.tasks.domain import Task

# ---------------------------------------------------------------------------
# Embedded sub-DTOs
# ---------------------------------------------------------------------------


class AssigneeDTO(BaseModel):
    """Wire shape for an assignee."""

    kind: Literal["human", "agent"]
    id: str
    name: str = ""


class SourceDTO(BaseModel):
    """Wire shape for the task's upstream source."""

    type: str = "user_request"
    ref_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class CreateTaskRequest(BaseModel):
    """Body for ``POST /tasks``.

    ``status`` is intentionally not on the request. The service derives
    it from ``assignee.kind`` — agent → ``proposed`` (claim flow);
    human → ``in_progress`` (already in someone's hands).

    ``blocked_by`` carries cloud Task ids this task depends on. The
    planner's two-pass materializer uses it to wire up TaskSpec
    ``blocked_by_keys`` after both endpoints exist; manual callers may
    pass it directly to register a dependency edge at create time.
    """

    title: str = Field(min_length=1, max_length=200)
    summary: str = ""
    assignee: AssigneeDTO
    pocket_id: str | None = None
    cycle_id: str | None = None
    project_id: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    kind: Literal["task", "nudge", "projection", "automation"] = "task"
    source: SourceDTO = Field(default_factory=SourceDTO)
    due_at: datetime | None = None


class UpdateTaskRequest(BaseModel):
    """Body for ``PATCH /tasks/{id}``. Every field is optional; only
    the keys the caller provides are touched.

    ``blocked_by`` is tri-state: ``None`` (omitted) leaves dependencies
    alone, ``[]`` explicitly clears them, a non-empty list replaces the
    full set. This lets a caller distinguish "I didn't pass it" from
    "I want it cleared" without a separate verb.
    """

    title: str | None = None
    summary: str | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    pocket_id: str | None = None
    cycle_id: str | None = None
    project_id: str | None = None
    blocked_by: list[str] | None = None
    due_at: datetime | None = None


class ClaimTaskRequest(BaseModel):
    """Body for ``POST /tasks/{id}/claim``. ``agent_id`` is the agent
    runtime's id of record — the claim only matches when this id equals
    the task's ``assignee.id`` and the task is still ``proposed``."""

    agent_id: str = Field(min_length=1)


class CompleteTaskRequest(BaseModel):
    """Body for ``POST /tasks/{id}/complete``.

    ``next_action`` picks the terminal status:
      - ``archive`` → status flips to ``done`` immediately.
      - ``request_approval`` → status flips to ``awaiting_approval`` so
        the Nudge surfaces in the creator's Tray for sign-off.
    """

    next_action: Literal["archive", "request_approval"] = "archive"
    result_summary: str = ""


class BlockTaskRequest(BaseModel):
    """Body for ``POST /tasks/{id}/block``."""

    reason: str = Field(min_length=1, max_length=500)


class ReassignTaskRequest(BaseModel):
    """Body for ``POST /tasks/{id}/reassign``. The new assignee carries
    its own kind so a human-to-agent or agent-to-human handoff updates
    routing in one call."""

    assignee_kind: Literal["human", "agent"]
    assignee_id: str = Field(min_length=1)
    assignee_name: str = ""


class BulkReassignRequest(BaseModel):
    """Body for ``POST /tasks/bulk-reassign`` (PR 1 façade may consume
    this directly; included here so the contract lives next to the
    other Tasks DTOs)."""

    task_ids: list[str] = Field(min_length=1)
    assignee_kind: Literal["human", "agent"]
    assignee_id: str = Field(min_length=1)
    assignee_name: str = ""


class ListTasksRequest(BaseModel):
    """Filters for ``GET /tasks``. All optional; the service applies
    them as a conjunction with the implicit ``workspace_id`` tenant
    filter."""

    assignee_id: str | None = None
    assignee_kind: Literal["human", "agent"] | None = None
    status: str | None = None
    cycle_id: str | None = None
    pocket_id: str | None = None
    project_id: str | None = None
    creator_id: str | None = None
    limit: int = Field(default=50, ge=1, le=500)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class TaskResponse(BaseModel):
    """Wire shape returned by every Task endpoint.

    Timestamps are ISO-8601 strings (always tz-aware, ``+00:00`` suffix)
    so the desktop client's ``new Date(...)`` parses unambiguously.
    """

    id: str
    workspace_id: str
    creator_id: str
    assignee: AssigneeDTO
    status: str
    priority: str
    kind: str
    source: SourceDTO
    title: str
    summary: str
    pocket_id: str | None = None
    cycle_id: str | None = None
    project_id: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    due_at: str | None = None
    blocked_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


def task_to_dto(task: Task) -> TaskResponse:
    """Map a domain :class:`Task` to its wire DTO.

    The mapping is mechanical because field names align — domain stays
    snake_case, the wire stays snake_case (Mission Control's frontend
    consumes either via the existing camelCase adapter shim).
    """

    return TaskResponse(
        id=task.id,
        workspace_id=task.workspace_id,
        creator_id=task.creator_id,
        assignee=AssigneeDTO(
            kind=task.assignee.kind,
            id=task.assignee.id,
            name=task.assignee.name,
        ),
        status=task.status,
        priority=task.priority,
        kind=task.kind,
        source=SourceDTO(
            type=task.source.type,
            ref_id=task.source.ref_id,
            metadata=dict(task.source.metadata),
        ),
        title=task.title,
        summary=task.summary,
        pocket_id=task.pocket_id,
        cycle_id=task.cycle_id,
        project_id=task.project_id,
        blocked_by=list(task.blocked_by),
        due_at=iso_utc(task.due_at),
        blocked_reason=task.blocked_reason,
        created_at=iso_utc(task.created_at),
        updated_at=iso_utc(task.updated_at),
    )


__all__ = [
    "AssigneeDTO",
    "BlockTaskRequest",
    "BulkReassignRequest",
    "ClaimTaskRequest",
    "CompleteTaskRequest",
    "CreateTaskRequest",
    "ListTasksRequest",
    "ReassignTaskRequest",
    "SourceDTO",
    "TaskResponse",
    "UpdateTaskRequest",
    "task_to_dto",
]
