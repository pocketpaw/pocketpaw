# project.py — Beanie document for the Projects entity.
# Created: 2026-05-16 — Mission Control backend completion. Projects are
#   a Linear-style scoping primitive: a body of work inside a workspace
#   that groups pockets, tasks, and cycles. The reference is optional on
#   every child entity — rows without a ``project_id`` keep working as
#   "unassigned" so the rollout is backwards-compatible.
"""Project document — Linear-style scoping primitive for Mission Control.

Only ``ee.cloud.projects.service`` may import this module; the
import-linter contract enforces the rule. Children (Pocket, Task, Cycle)
keep a denormalised ``project_id: str | None`` rather than a foreign-key
ref so the change is purely additive and the existing query paths stay
untouched.
"""

from __future__ import annotations

from beanie import Indexed
from pydantic import Field

from ee.cloud.models.base import TimestampedDocument


class Project(TimestampedDocument):
    """Project — a workspace-scoped container for pockets, tasks, cycles.

    Status machine is intentionally tiny: ``active`` and ``archived``.
    Soft-archive lets the UI hide a project from the default picker while
    keeping historical references resolvable (you can still click into
    last quarter's project from a Pawprint and see the rows underneath).
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    name: str
    description: str = ""
    color: str = ""  # hex string, optional
    lead_id: str | None = None  # workspace member id; None when unassigned
    status: str = Field(default="active", pattern="^(active|archived)$")
    created_by: str = ""

    class Settings:
        name = "projects"
        indexes = [
            [("workspace", 1), ("status", 1)],
        ]


__all__ = ["Project"]
