# domain.py — frozen value objects for the Projects entity.
# Created: 2026-05-16 — Mission Control backend completion. Projects are a
#   Linear-style scoping primitive: a body of work inside a workspace that
#   bundles pockets, tasks, and cycles together. Domain objects are pure
#   Python so services can be tested without Beanie; the service layer
#   converts to/from the Mongo doc.
"""Domain value objects for the Projects entity.

Pure-Python frozen dataclasses. ``projects/service.py`` owns the
Beanie ↔ domain conversion. Tenancy fields (``workspace_id``) are
required at construction time — domain objects without one are a type
error, which catches the simplest cross-tenant leak.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NewType

ProjectId = NewType("ProjectId", str)
"""Wrapper newtype for project ids so the type checker can spot accidental
swaps with arbitrary strings (pocket ids, task ids, etc.) at call sites
that surface ``project_id`` through public APIs."""

ProjectStatus = Literal["active", "archived"]
"""Project lifecycle.

``active``   — visible in pickers, accepts new pockets/tasks/cycles.
``archived`` — soft-archived; hidden from default lists but still
               addressable by id (so historical pockets/tasks/cycles can
               still resolve their project reference).
"""


@dataclass(frozen=True)
class Project:
    """A scoping container inside a workspace.

    Required tenancy fields (``workspace_id``, ``name``) have no defaults
    — constructing a domain ``Project`` without them is a TypeError. All
    other fields are optional metadata used by the Mission Control UI
    when grouping the feed by project.
    """

    id: ProjectId
    workspace_id: str
    name: str
    description: str = ""
    color: str = ""  # hex string, optional
    lead_id: str | None = None  # workspace member id; None when unassigned
    status: ProjectStatus = "active"
    created_by: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = ["Project", "ProjectId", "ProjectStatus"]
