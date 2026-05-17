# Created: 2026-05-17 — pocketpaw#1118 P1. Frozen domain value objects
#   for plan sessions and the agent-gap signal surfaced back to the
#   operator UI. Both are constructed only by ``planner.service`` after
#   a successful OSS planner run; routers + tests consume the DTO wire
#   shapes in ``planner.dto``.
"""Planner entity — domain value objects.

A :class:`PlanSession` is the cloud-side record of one OSS planner run.
It is workspace-scoped (Rule 3: tenancy required at construction time)
and pins to a single cloud :class:`Project` so re-plans replace the
prior session for that project rather than accumulating duplicates.

An :class:`AgentGap` is a derived diagnostic — for every recommended
agent the OSS planner returned that does *not* match an existing cloud
:class:`Agent` in the workspace, we surface one row so the operator can
either claim the gap (create the agent) or accept the human fallback
the planner already wrote into the materialized tasks.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanSession:
    """A single planner invocation, scoped to a workspace + project.

    ``id`` is the OSS planner's session id (currently the project_id we
    handed in) — exposed verbatim so we can correlate cloud-side records
    back to OSS deep_work runs if a debug round-trip is ever needed.

    Tenancy fields are required (no defaults) so a value object built
    without ``workspace_id`` or ``project_id`` is a type error at
    construction, not a silent leak at the read path.
    """

    id: str
    workspace_id: str
    project_id: str
    status: str
    prd_file_id: str | None
    plan_file_id: str | None
    goal_file_id: str | None
    task_ids: tuple[str, ...]
    agent_gaps: tuple[AgentGap, ...]


@dataclass(frozen=True)
class AgentGap:
    """A planner-recommended agent the workspace does not yet have.

    ``spec_name`` is the agent name the planner suggested. ``recommended_role``
    is the human-readable role string the planner produced; the operator UI
    renders both so the captain sees what the planner *meant* before
    deciding whether to materialize the agent or accept the fallback.
    """

    spec_name: str
    recommended_role: str
    specialties: tuple[str, ...]


__all__ = ["AgentGap", "PlanSession"]
