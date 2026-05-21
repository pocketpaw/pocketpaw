# task.py — Beanie document for the Mission Control Tasks entity.
# Created: 2026-05-13 — PR 2 of 3 for Mission Control's backend. Tasks
#   are the unified work-item primitive that subsumes Nudges + agent
#   tasks + Pawprint projections with assignee polymorphism (human or
#   agent). Tied to a Pocket optionally, a Cycle optionally, and a
#   workspace always. Indexes back the two hot query paths:
#     - "list my work" by (workspace_id, assignee_id, status)
#     - "items in this cycle" by (workspace_id, cycle_id)
# Updated: 2026-05-17 (feat/planner-gaps-and-deps) — pocketpaw#1118 P4
#   added ``blocked_by: list[str]`` carrying cloud Task ids this task
#   depends on. No migration script — Mongo absorbs the new field on
#   next write; reads of old docs default to ``[]``.
"""Task document — Mission Control work-item primitive.

Embedded sub-documents:

  - :class:`TaskAssignee` — polymorphic kind (``human`` | ``agent``) +
    id + display name. Kept in one document so list queries don't need
    a join to render the assignee chip in the feed.
  - :class:`TaskSource` — a discriminated union pointer to the upstream
    that produced this task: ``user_request`` (manual), ``nudge``
    (Instinct proposal), ``agent`` (sub-task spawned by an agent),
    ``automation`` (rule-driven), etc.

Only ``ee.cloud.tasks.service`` may import this module; the import-linter
contract enforces the rule.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Indexed
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class TaskAssignee(BaseModel):
    """Polymorphic assignee. ``kind`` decides how the runtime treats the
    work — human assignees notify, agent assignees enqueue."""

    kind: str = Field(pattern="^(human|agent)$")
    id: str
    name: str = ""


class TaskSource(BaseModel):
    """Where this task came from. ``type`` is the discriminator; ``ref_id``
    points at the source entity when one exists (e.g. the Nudge id when
    ``type == 'nudge'``)."""

    type: str = "user_request"
    ref_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Task(TimestampedDocument):
    """A unit of work assignable to a human or an agent.

    Status machine:
        proposed → in_progress → done | reverted | failed
                 → awaiting_approval → done | reverted
                 → blocked → in_progress | failed

    A Nudge is just a Task with ``status == 'awaiting_approval'``.
    Tasks created for an agent default to ``proposed`` so the agent
    runtime can pick them up via the claim path; tasks for a human
    default to ``in_progress`` (they're already assigned).
    """

    workspace_id: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str | None = None
    cycle_id: str | None = None
    project_id: str | None = None
    creator_id: str

    title: str
    summary: str = ""

    assignee: TaskAssignee
    # Denormalised ``assignee.id`` / ``assignee.kind`` for index hits
    # without a sub-field lookup. The service writes both fields whenever
    # ``assignee`` changes; we keep these explicit so the
    # ``(workspace_id, assignee_id, status)`` compound index is usable.
    assignee_id: Indexed(str)  # type: ignore[valid-type]
    assignee_kind: str = Field(pattern="^(human|agent)$")

    status: str = Field(
        default="proposed",
        pattern="^(proposed|in_progress|awaiting_approval|done|reverted|failed|blocked)$",
    )
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")
    kind: str = Field(default="task", pattern="^(task|nudge|projection|automation)$")

    source: TaskSource = Field(default_factory=TaskSource)

    # Task ids this task depends on. Populated by the planner's two-pass
    # materializer for ``TaskSpec.blocked_by_keys`` and by direct callers
    # that pass ``blocked_by`` on Create/Update. Plain string list — we
    # don't enforce referential integrity at the DB layer because cross-
    # workspace ids are already filtered out by the tenant guard on every
    # service write.
    blocked_by: list[str] = Field(default_factory=list)

    due_at: datetime | None = None
    blocked_reason: str | None = None

    class Settings:
        name = "tasks"
        indexes = [
            [("workspace_id", 1), ("assignee_id", 1), ("status", 1)],
            [("workspace_id", 1), ("cycle_id", 1)],
            [("workspace_id", 1), ("status", 1), ("createdAt", -1)],
        ]


__all__ = ["Task", "TaskAssignee", "TaskSource"]
