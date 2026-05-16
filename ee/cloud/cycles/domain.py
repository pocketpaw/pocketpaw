"""Cycles domain — frozen value objects.

Pure Python dataclasses, no Beanie / Pydantic / FastAPI imports. The
service maps between these and the ``Cycle`` Beanie document; the DTO
layer maps these to Pydantic responses.

Multi-tenancy is enforced at construction: ``workspace_id`` is required
positionally with no default — building a ``Cycle`` without one is a type
error. Same rule as the rest of ``ee/cloud``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

CycleStatus = Literal["active", "upcoming", "completed"]


@dataclass(frozen=True)
class CycleDailyPoint:
    """One day's snapshot of cycle counters.

    Captured by the daily snapshot job. ``is_weekend`` is recorded at
    snapshot time so the burnup chart can flatten the ideal target line
    over weekends without a calendar round-trip on read.
    """

    date: date
    scope: int = 0
    started: int = 0
    completed: int = 0
    is_weekend: bool = False


@dataclass(frozen=True)
class Cycle:
    """Cycle value object — a time-boxed work window inside a workspace.

    ``pocket_id`` is optional: a cycle can span multiple pockets (an
    engagement may pull on multiple ops surfaces). When set, the cycle is
    primarily filtered against that pocket; tasks across other pockets in
    the same workspace can still attach via ``cycle_id`` on the task.

    Counters (``scope`` / ``started`` / ``completed``) are denormalized
    for cheap list reads. The detail endpoint refreshes them from Tasks
    at fetch time; the snapshot job captures historical values into
    ``daily``.
    """

    id: str
    workspace_id: str
    name: str
    description: str
    pocket_id: str | None
    start: date
    end: date
    status: CycleStatus
    scope: int = 0
    started: int = 0
    completed: int = 0
    daily: tuple[CycleDailyPoint, ...] = field(default_factory=tuple)
    created_by: str = ""
    project_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["Cycle", "CycleDailyPoint", "CycleStatus"]
