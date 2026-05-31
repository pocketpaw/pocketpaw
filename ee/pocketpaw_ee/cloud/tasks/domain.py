# domain.py — frozen value objects for the Tasks entity.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend. Tasks
#   are the unified work-item primitive used by Mission Control's
#   feed. Domain objects are plain Python so services can be tested
#   without Beanie; the service layer converts to/from the Mongo doc.
# Updated: 2026-05-17 (feat/planner-gaps-and-deps) — pocketpaw#1118 P4
#   added the ``blocked_by`` field carrying cloud Task ids this task
#   depends on. Tuple stays read-only on the frozen dataclass; the
#   service writes it via ``agent_create_task`` + ``agent_update_task``.
# Updated: 2026-05-21 (feat/taskspec-success-criteria) — added
#   ``success_criteria`` and ``preconditions`` tuples carrying the
#   machine-verifiable criteria the planner emits on each TaskSpec.
#   Read-only on the frozen dataclass; the service maps them from the
#   Beanie doc. Unblocks completion-time verification (pocketpaw#1162).
"""Domain value objects for the Tasks entity.

Pure-Python frozen dataclasses. ``tasks/service.py`` owns the
Beanie ↔ domain conversion. Tenancy fields are required at construction
time — domain objects without ``workspace_id`` are a type error, which
catches the simplest cross-tenant leak.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

TaskStatus = Literal[
    "proposed",
    "in_progress",
    "awaiting_approval",
    "done",
    "reverted",
    "failed",
    "blocked",
]
"""The Task status machine.

``proposed``       — created, not yet picked up. Default for agent assignees.
``in_progress``    — work is happening. Default for human assignees on create.
``awaiting_approval`` — Nudge-flavoured: work is done, creator must approve.
``done``           — terminal: shipped and (optionally) approved.
``reverted``       — terminal: approver rejected the work product.
``failed``         — terminal: agent gave up; surfaces in the operator's Snags.
``blocked``        — non-terminal pause with a reason; recoverable.
"""

TaskPriority = Literal["low", "normal", "high", "urgent"]
TaskKind = Literal["task", "nudge", "projection", "automation"]
AssigneeKind = Literal["human", "agent"]


@dataclass(frozen=True)
class TaskAssignee:
    """Polymorphic assignee. ``kind`` decides routing behaviour.

    Storing ``name`` denormalised next to ``id`` lets the feed render
    the assignee chip without a join. The service refreshes ``name`` on
    every reassignment.
    """

    kind: AssigneeKind
    id: str
    name: str = ""


@dataclass(frozen=True)
class TaskSource:
    """Discriminated union pointer to the upstream that produced this
    task. ``type`` is the discriminator (``user_request``, ``nudge``,
    ``agent``, ``automation`` — extensible). ``ref_id`` points at the
    source entity when one exists."""

    type: str = "user_request"
    ref_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Task:
    """A unit of work assignable to a human or an agent.

    Required tenancy fields (``workspace_id``, ``creator_id``,
    ``assignee``) have no defaults — constructing a domain ``Task``
    without them is a TypeError. ``pocket_id`` and ``cycle_id`` are
    legitimately optional (workspace-scoped tasks not tied to a single
    pocket or cycle).
    """

    id: str
    workspace_id: str
    creator_id: str
    assignee: TaskAssignee
    status: TaskStatus
    priority: TaskPriority
    kind: TaskKind
    source: TaskSource
    title: str
    summary: str
    pocket_id: str | None = None
    cycle_id: str | None = None
    project_id: str | None = None
    blocked_by: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    due_at: datetime | None = None
    blocked_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = [
    "AssigneeKind",
    "Task",
    "TaskAssignee",
    "TaskKind",
    "TaskPriority",
    "TaskSource",
    "TaskStatus",
]
