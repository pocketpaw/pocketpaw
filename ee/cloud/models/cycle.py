"""Cycle document — time-boxed work window for Mission Control.

A Cycle is the events-production analogue of a Linear "cycle": a 4-6 week
prep window for one engagement (May 23 wedding, June 8 corporate summit,
etc.). Tasks reference back to a Cycle via ``cycle_id``.

The ``daily`` array is embedded (cap ~100 entries) — one point per day
captured by ``ee.cloud.cycles.snapshot_job`` for the burnup chart. Beyond
the cap the snapshot job downgrades to weekly cadence so a long-running
cycle doesn't blow the document size budget.

Only ``ee.cloud.cycles.service`` may import this module — enforced by the
``import-linter`` contract in ``pyproject.toml``.
"""

from __future__ import annotations

from datetime import date

from beanie import Indexed
from pydantic import BaseModel, Field

from ee.cloud.models.base import TimestampedDocument


class CycleDailyPoint(BaseModel):
    """One day's snapshot of cycle scope/started/completed counts.

    ``is_weekend`` is captured at snapshot time so the burnup chart can
    flatten the ideal target line over weekends without needing to recompute
    the calendar on read.
    """

    date: date
    scope: int = 0
    started: int = 0
    completed: int = 0
    is_weekend: bool = False


class Cycle(TimestampedDocument):
    """Time-boxed work window for Mission Control.

    Counters (``scope`` / ``started`` / ``completed``) are denormalized for
    cheap list reads; the snapshot job is the source of truth for historical
    series, and ``ee.cloud.cycles.service.agent_get_cycle`` refreshes them
    from the Tasks collection at fetch time.
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    description: str = ""
    # A cycle can span multiple pockets — ``pocket_id`` is the *primary*
    # pocket for filtering; tasks with any pocket assignment can reference
    # the cycle as long as the workspace matches.
    pocket_id: str | None = None
    # Optional Mission Control Project the cycle is grouped under. Same
    # backwards-compat story as Pocket / Task — None means "no project
    # assigned" so existing cycles read back unchanged.
    project_id: str | None = None
    start: date
    end: date
    status: str = Field(default="upcoming", pattern="^(active|upcoming|completed)$")
    # Denormalized counters — kept fresh by service writes; readers should
    # treat them as approximate and recompute on the detail endpoint.
    scope: int = 0
    started: int = 0
    completed: int = 0
    daily: list[CycleDailyPoint] = Field(default_factory=list)
    # Audit: who created this cycle.
    created_by: str = ""

    class Settings:
        name = "cycles"
        indexes = [
            [("workspace", 1), ("status", 1)],
            [("workspace", 1), ("pocket_id", 1), ("status", 1)],
        ]
